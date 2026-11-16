use agent_desktop_core::AdapterError;
use zbus::Connection;
use zbus::zvariant::OwnedValue;

use super::liveness::Liveness;
use super::node_read::{COMPONENT_INTERFACE, call, deserialize, field};
use crate::tree::element::bounded::collect_bounded;
use crate::tree::{AtspiElement, Extents, Field};

const ACTION_INTERFACE: &str = "org.a11y.atspi.Action";
const ACTION_NAMES_IN_FLIGHT: usize = 4;
const MAX_ACTIONS: i32 = 256;
const SCREEN_COORDINATES: u32 = 0;

/// `None` when the interface is advertised and its reads should go ahead.
/// When the interface list itself could not be read, nothing is known about
/// the interface, so the dependent field is a failure and not an absence.
pub(in crate::tree) fn skipped_without<T>(
    interfaces: &Field<Vec<String>>,
    name: &str,
) -> Option<Field<T>> {
    match interfaces {
        Field::Present(list) if list.iter().any(|entry| entry == name) => None,
        Field::Present(_) | Field::Absent => Some(Field::Absent),
        Field::Failed(detail) => Some(Field::Failed(format!(
            "interface list unavailable, {name} not checked: {detail}"
        ))),
    }
}

pub(in crate::tree) async fn read_extents(
    connection: &Connection,
    element: &AtspiElement,
    interfaces: &Field<Vec<String>>,
    liveness: Liveness,
) -> Result<Field<Extents>, AdapterError> {
    if let Some(skipped) = skipped_without(interfaces, COMPONENT_INTERFACE) {
        return Ok(skipped);
    }
    let reply = call(
        connection,
        element,
        COMPONENT_INTERFACE,
        "GetExtents",
        &(SCREEN_COORDINATES,),
    )
    .await;
    field(deserialize::<(i32, i32, i32, i32)>(reply), liveness).map(|read| {
        read.map(|(x, y, width, height)| Extents {
            x,
            y,
            width,
            height,
        })
    })
}

async fn action_name(
    connection: &Connection,
    element: &AtspiElement,
    index: i32,
    liveness: Liveness,
) -> Result<Field<String>, AdapterError> {
    let reply = call(connection, element, ACTION_INTERFACE, "GetName", &(index,)).await;
    field(deserialize::<String>(reply), liveness)
}

/// Reads indexed machine names through `NActions` and `GetName(i)`. The bulk
/// `GetActions` method can abort older ATK bridges while packing its reply.
/// An unreadable name leaves the whole field unknown rather than shifting
/// the indices that `DoAction` consumes. Connection loss ends the read.
pub(in crate::tree) async fn read_actions(
    connection: &Connection,
    element: &AtspiElement,
    interfaces: &Field<Vec<String>>,
    liveness: Liveness,
) -> Result<Field<Vec<String>>, AdapterError> {
    if let Some(skipped) = skipped_without(interfaces, ACTION_INTERFACE) {
        return Ok(skipped);
    }
    let reply = call(
        connection,
        element,
        crate::system::PROPERTIES_INTERFACE,
        "Get",
        &(ACTION_INTERFACE, "NActions"),
    )
    .await;
    let count = match field(deserialize::<OwnedValue>(reply), liveness)? {
        Field::Present(value) => match value.downcast_ref::<i32>() {
            Ok(count) if (0..=MAX_ACTIONS).contains(&count) => count,
            Ok(count) => {
                return Ok(Field::Failed(format!(
                    "NActions {count} is outside the supported range 0..={MAX_ACTIONS}"
                )));
            }
            Err(error) => return Ok(Field::Failed(format!("Invalid NActions: {error}"))),
        },
        Field::Absent => return Ok(Field::Failed("NActions was not read".to_owned())),
        Field::Failed(detail) => return Ok(Field::Failed(detail)),
    };
    let reads = collect_bounded(
        ACTION_NAMES_IN_FLIGHT,
        (0..count).map(|index| action_name(connection, element, index, liveness)),
    )
    .await
    .into_iter()
    .collect::<Result<Vec<_>, _>>()?;
    let mut names = Vec::with_capacity(reads.len());
    for (index, read) in reads.into_iter().enumerate() {
        match read {
            Field::Present(name) => names.push(name),
            Field::Failed(detail) => {
                return Ok(Field::Failed(format!("GetName({index}): {detail}")));
            }
            Field::Absent => {
                return Ok(Field::Failed(format!("GetName({index}) was not read")));
            }
        }
    }
    Ok(Field::Present(names))
}

#[cfg(test)]
#[path = "interface_reads_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "action_enumeration_tests.rs"]
mod action_enumeration_tests;
