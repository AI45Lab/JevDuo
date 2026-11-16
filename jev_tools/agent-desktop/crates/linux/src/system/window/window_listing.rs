use agent_desktop_core::{AdapterError, Deadline, Rect, WindowInfo, WindowState};
use zbus::Connection;

use crate::system::bus::bus_slice::within_slice;
use crate::system::registry::frame_read::FrameRead;
use crate::system::registry::registry_frames::CHILDREN_SLICE;
use crate::system::{ACCESSIBLE_INTERFACE, SharedBus, within};

use super::window_filter::frame_state;
use super::window_id;
use crate::system::registry::listed_window::ListedWindow;
use crate::system::registry::registry_app::RegistryApp;
use crate::system::x11::FrameFacts;
use crate::tree::properties::toolkit_trust::reports_global_position;
use crate::tree::surfaces::top_level::{
    is_dialog_role, is_menu_wrapper, is_popup_menu_candidate, is_popup_menu_surface, is_top_level,
};
use crate::tree::{AtspiElement, RawNode, call, list_children};

fn listed(
    index: usize,
    app: &RegistryApp,
    node: &RawNode,
    siblings: &[RawNode],
) -> Option<ListedWindow> {
    if !is_top_level(&node.traits.interfaces) {
        return None;
    }
    let title = node.properties.name.present().cloned().unwrap_or_default();
    let id = window_id::encode(node.element.bus_name(), node.element.path())?;
    let state = frame_state(&node.traits.states);
    let global_position = reports_global_position(&app.toolkit);
    Some(ListedWindow {
        app: index,
        info: WindowInfo {
            id,
            title: title.clone(),
            app: app.name.clone(),
            pid: app.root.pid().into(),
            process_instance: Some(app.facts.instance.clone()),
            bounds: global_position
                .then(|| node.extents.present().copied().map(Rect::from))
                .flatten(),
            state: WindowState {
                is_focused: false,
                accessible: true,
                minimized: state.minimized,
                visible: state.visible,
            },
        },
        active: state.active == Some(true),
        modal: state.modal == Some(true),
        dialog: is_dialog_role(node.traits.role.present()),
        frame: FrameFacts {
            size_identity: super::frame_identity::permits_size_match(
                node,
                siblings,
                &app.toolkit,
                node.extents.present().copied(),
            ),
            pid: app.root.pid(),
            title,
            extents: node.extents.present().copied(),
            global_position,
        },
        sheet: crate::tree::surfaces::is_sheet(node),
        unshown: state.visible == Some(false) && state.minimized == Some(false),
    })
}

/// The windows among the frames read for each selected application. `apps`
/// pairs a position in the caller's list with the application, in the order
/// `frames` was read.
pub(crate) fn collect(
    apps: &[(usize, &RegistryApp)],
    frames: &[Vec<RawNode>],
) -> Vec<ListedWindow> {
    apps.iter()
        .zip(frames)
        .flat_map(|((index, app), nodes)| {
            nodes
                .iter()
                .filter_map(|node| listed(*index, app, node, nodes))
        })
        .collect()
}

async fn child_role(connection: &Connection, element: &AtspiElement) -> Option<u32> {
    call(connection, element, ACCESSIBLE_INTERFACE, "GetRole", &())
        .await
        .ok()?
        .body()
        .deserialize()
        .ok()
}

async fn popup_children(connection: &Connection, node: &RawNode) -> Option<Vec<(u32, Vec<u32>)>> {
    use crate::tree::vocabulary::role_numbers::MENU_BAR;
    let mut budget = 80;
    let children = popup_level(connection, &node.element, &mut budget).await?;
    let mut roles = Vec::new();
    for (child, role) in children {
        let mut menus = Vec::new();
        if role == MENU_BAR {
            for (_, role) in popup_level(connection, &child, &mut budget).await? {
                menus.push(role);
            }
        }
        roles.push((role, menus));
    }
    Some(roles)
}

async fn popup_level(
    connection: &Connection,
    root: &AtspiElement,
    budget: &mut usize,
) -> Option<Vec<(AtspiElement, u32)>> {
    let mut pending = list_children(connection, root).await.ok()?;
    let mut children = Vec::new();
    while let Some((bus, path)) = pending.pop() {
        *budget = budget.checked_sub(1)?;
        if bus != root.bus_name() {
            return None;
        }
        let child = AtspiElement::new(bus, path, root.pid());
        let role = child_role(connection, &child).await?;
        if is_menu_wrapper(role) {
            pending.extend(list_children(connection, &child).await.ok()?);
        } else {
            children.push((child, role));
        }
    }
    Some(children)
}

fn exclude_popup_menus(
    nodes: &mut Vec<RawNode>,
    mut read: impl FnMut(&RawNode) -> Option<Vec<(u32, Vec<u32>)>>,
) {
    nodes.retain(|node| {
        if !is_popup_menu_candidate(node) {
            return true;
        }
        !is_popup_menu_surface(node, read(node).as_deref())
    });
}

/// Reads extra child roles only for blank showing top-levels. Failed popup
/// classification keeps the window, using the listing's original deadline.
pub(crate) fn read_frames(
    bus: &SharedBus,
    roots: &[AtspiElement],
    deadline: Deadline,
) -> Result<Vec<FrameRead>, AdapterError> {
    let mut frames = crate::system::registry::registry_frames::read_frames(bus, roots, deadline)?;
    for frame in &mut frames {
        exclude_popup_menus(&mut frame.nodes, |node| {
            let connection = bus.get(deadline).ok()?;
            within(deadline, async {
                Ok(
                    within_slice(CHILDREN_SLICE, popup_children(connection.inner(), node))
                        .await
                        .flatten(),
                )
            })
            .ok()
            .flatten()
        });
    }
    Ok(frames)
}

#[cfg(test)]
#[path = "window_listing_tests.rs"]
mod tests;
