use std::cell::{Cell, RefCell};

use super::*;

struct Port {
    value: RefCell<String>,
    selected: bool,
    writes: Cell<usize>,
    fail_write: bool,
    ignore_write: bool,
}

impl Replacement for Port {
    fn value(&self) -> Result<String, AdapterError> {
        Ok(self.value.borrow().clone())
    }

    fn select_all(&self) -> Result<Vec<ActionStep>, AdapterError> {
        Ok(vec![
            ActionStep::succeeded("select_all").with_verified(false),
        ])
    }

    fn selected(&self, _: &str) -> Result<bool, AdapterError> {
        Ok(self.selected)
    }

    fn write(&self) -> Result<Vec<ActionStep>, AdapterError> {
        self.writes.set(self.writes.get() + 1);
        if self.fail_write {
            return Err(AdapterError::timeout("partial write")
                .with_disposition(DeliverySemantics::uncertain()));
        }
        if !self.ignore_write {
            *self.value.borrow_mut() = "new".to_owned();
        }
        Ok(vec![ActionStep::succeeded("type").with_verified(false)])
    }
}

fn port() -> Port {
    Port {
        value: RefCell::new("old".to_owned()),
        selected: true,
        writes: Cell::new(0),
        fail_write: false,
        ignore_write: false,
    }
}

#[test]
fn replacement_is_verified_only_after_the_exact_value_is_read() {
    let port = port();
    let result = replace(&port, "new").unwrap();
    assert_eq!(
        result.disposition(),
        DeliverySemantics::delivered_verified()
    );
    assert_eq!(port.writes.get(), 1);
    assert_eq!(port.value.borrow().as_str(), "new");
    assert_eq!(
        result.details.unwrap()["verification_scope"],
        "element_value"
    );
}

#[test]
fn an_already_satisfied_value_sends_no_keys() {
    let port = port();
    let result = replace(&port, "old").unwrap();
    assert_eq!(result.disposition(), DeliverySemantics::not_delivered());
    assert!(result.steps.is_empty());
    assert_eq!(port.writes.get(), 0);
}

#[test]
fn an_unproven_selection_prevents_the_replacement() {
    let mut port = port();
    port.selected = false;
    let error = replace(&port, "new").unwrap_err();
    assert_eq!(port.writes.get(), 0);
    assert_eq!(error.disposition, DeliverySemantics::delivered_unverified());
    assert_eq!(error.details.unwrap()["physical_delivery_started"], true);
    assert_eq!(port.value.borrow().as_str(), "old");
}

#[test]
fn uncertain_delivery_is_preserved_and_never_repeated() {
    let mut port = port();
    port.fail_write = true;
    let error = replace(&port, "new").unwrap_err();
    assert_eq!(error.disposition, DeliverySemantics::uncertain());
    assert_eq!(port.writes.get(), 1);
}

#[test]
fn a_dropped_write_is_not_reported_as_success_or_safe_to_repeat() {
    let mut port = port();
    port.ignore_write = true;
    let error = replace(&port, "new").unwrap_err();
    assert_eq!(error.code, ErrorCode::ActionFailed);
    assert_eq!(error.disposition, DeliverySemantics::delivered_unverified());
    assert_eq!(port.writes.get(), 1);
    let steps = error.details.unwrap()["completed_steps"]
        .as_array()
        .unwrap()
        .clone();
    assert_eq!(steps.len(), 2);
    assert_eq!(steps[1]["label"], "type");
}
