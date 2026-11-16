use std::time::Duration;

use agent_desktop_core::{
    Action, ActionResult, ActionStep, AdapterError, DeliverySemantics, ErrorCode, InteractionLease,
    InteractionPolicy, KeyCombo, Modifier,
};
use serde_json::json;

use super::super::context::Target;
use crate::input::physical::target_keyboard::{focused_target_steps, target_steps, validate_text};
use crate::input::physical::target_world::LiveWorld;
use crate::tree::capabilities::text_selection::read_selection;
use crate::tree::vocabulary::role_numbers::{COMBO_BOX, ENTRY, TEXT};
use crate::tree::vocabulary::state_bits::{EDITABLE, READ_ONLY};

trait Replacement {
    fn value(&self) -> Result<String, AdapterError>;
    fn select_all(&self) -> Result<Vec<ActionStep>, AdapterError>;
    fn selected(&self, before: &str) -> Result<bool, AdapterError>;
    fn write(&self) -> Result<Vec<ActionStep>, AdapterError>;
    fn verified(&self, text: &str) -> Result<bool, AdapterError> {
        Ok(self.value()? == text)
    }
}

fn refused(message: &str) -> AdapterError {
    AdapterError::new(ErrorCode::ActionNotSupported, message)
        .with_details(json!({ "kind": "physical_text_replacement_unproven" }))
        .with_disposition(DeliverySemantics::not_delivered())
}

fn after_selection(mut error: AdapterError, steps: &[ActionStep]) -> AdapterError {
    if error.disposition != DeliverySemantics::uncertain() {
        error.disposition = DeliverySemantics::delivered_unverified();
    }
    let mut details = match error.details.take() {
        Some(serde_json::Value::Object(details)) => details,
        Some(other) => serde_json::Map::from_iter([("cause_details".to_owned(), other)]),
        None => serde_json::Map::new(),
    };
    details.insert("physical_delivery_started".to_owned(), json!(true));
    details.insert("completed_steps".to_owned(), json!(steps));
    error.details = Some(serde_json::Value::Object(details));
    error
}

fn replace(port: &impl Replacement, text: &str) -> Result<ActionResult, AdapterError> {
    let before = port.value()?;
    if before == text {
        return Ok(ActionResult::satisfied_without_delivery("set-value"));
    }
    let mut steps = port.select_all()?;
    let mut finish = || {
        if !port.selected(&before)? {
            return Err(refused(
                "The field's entire unchanged value was not proven selected",
            ));
        }
        let written = port.write()?;
        steps.extend(written);
        if !port.verified(text)? {
            return Err(AdapterError::new(
                ErrorCode::ActionFailed,
                "The field does not hold the requested replacement text",
            ));
        }
        if let Some(last) = steps.pop() {
            steps.push(last.with_verified(true));
        }
        Ok(())
    };
    finish().map_err(|error| after_selection(error, &steps))?;
    Ok(
        ActionResult::from_execution(&Action::SetValue(String::new()), steps)
            .with_verified_delivery()
            .with_details(json!({
                "verification_scope": "element_value",
                "application_commit": "not_verified",
                "route": "physical_text_replacement",
            })),
    )
}

struct LiveReplacement<'a, 'b> {
    target: &'a Target<'b>,
    text: &'a str,
    lease: &'a InteractionLease,
}

impl LiveReplacement<'_, '_> {
    fn send(&self, action: Action) -> Result<Vec<ActionStep>, AdapterError> {
        target_steps(
            &LiveWorld {
                bus: self.target.bus,
                lease: self.lease,
            },
            self.target.element,
            &action,
            InteractionPolicy::headed(),
            self.target.deadline,
        )
    }

    fn read(&self) -> Result<String, AdapterError> {
        let view = self.target.view()?;
        if !matches!(view.role(), Some(ENTRY | TEXT | COMBO_BOX))
            || view.state(EDITABLE) != Some(true)
            || view.state(READ_ONLY) != Some(false)
            || view.secure()
        {
            return Err(refused(
                "Physical replacement needs a proven editable, nonsecure text field",
            ));
        }
        view.value()
            .map(str::to_owned)
            .ok_or_else(|| refused("Physical replacement needs a complete readable field value"))
    }
}

impl Replacement for LiveReplacement<'_, '_> {
    fn value(&self) -> Result<String, AdapterError> {
        self.read()
    }

    fn select_all(&self) -> Result<Vec<ActionStep>, AdapterError> {
        self.send(Action::PressKey(KeyCombo {
            key: "a".to_owned(),
            modifiers: vec![Modifier::Ctrl],
        }))
    }

    fn selected(&self, before: &str) -> Result<bool, AdapterError> {
        let selected = || {
            if self.read()? != before {
                return Ok(false);
            }
            let range = read_selection(self.target.bus, self.target.handle, self.target.deadline)?;
            Ok(range == Some(0..before.encode_utf16().count()))
        };
        super::super::click::poll_until(
            self.target.deadline,
            Duration::from_millis(500),
            Duration::from_millis(25),
            selected,
            |read| matches!(read, Ok(true)),
        )
    }

    fn write(&self) -> Result<Vec<ActionStep>, AdapterError> {
        let action = if self.text.is_empty() {
            Action::PressKey(KeyCombo {
                key: "backspace".to_owned(),
                modifiers: Vec::new(),
            })
        } else {
            Action::TypeText(self.text.to_owned())
        };
        focused_target_steps(
            &LiveWorld {
                bus: self.target.bus,
                lease: self.lease,
            },
            self.target.element,
            &action,
            self.target.deadline,
        )
    }

    fn verified(&self, text: &str) -> Result<bool, AdapterError> {
        super::super::click::poll_until(
            self.target.deadline,
            Duration::from_millis(500),
            Duration::from_millis(25),
            || self.read().map(|read| read == text),
            |read| matches!(read, Ok(true)),
        )
    }
}

pub(crate) fn execute(
    target: &Target<'_>,
    text: &str,
    lease: &InteractionLease,
) -> Result<ActionResult, AdapterError> {
    validate_text(
        &LiveWorld {
            bus: target.bus,
            lease,
        },
        text,
        target.deadline,
    )?;
    replace(
        &LiveReplacement {
            target,
            text,
            lease,
        },
        text,
    )
}

#[cfg(test)]
#[path = "physical_replace_tests.rs"]
mod tests;
