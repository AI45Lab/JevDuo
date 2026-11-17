use super::*;
use crate::system::process::process_facts::ProcessFacts;
use crate::system::process::toolkit_id::ToolkitId;
use crate::system::registry::registry_app::assemble;
use crate::tree::live::test_support::{attributes, read_of, words};
use crate::tree::{AtspiElement, Extents, Field};

fn app(toolkit: Option<(&str, &str)>) -> RegistryApp {
    assemble(
        AtspiElement::new(":1.9", "/org/a11y/atspi/accessible/root", 321),
        ProcessFacts {
            executable: None,
            comm: "x".to_owned(),
            exe_basename: Some("x".to_owned()),
            instance: "boot:42".to_owned(),
        },
        Some(Some("App".to_owned())),
        ToolkitId {
            name: toolkit.map(|(name, _)| name.to_owned()),
            version: toolkit.map(|(_, version)| version.to_owned()),
        },
    )
}

fn frame(role: u32, name: &str, path: &str, state_bits: &[u32]) -> RawNode {
    let mut node = read_of(role, name, &[]).node;
    node.element = AtspiElement::new(":1.9", path, 321);
    node.traits.interfaces = Field::Present(vec![crate::tree::COMPONENT_INTERFACE.to_owned()]);
    node.traits.states = Field::Present(words(state_bits));
    node
}

#[test]
fn a_shown_active_frame_becomes_a_window_with_the_process_instance() {
    let app = app(Some(("gtk", "3.24")));
    let nodes = vec![frame(23, "Doc", "/org/a11y/atspi/accessible/1", &[25, 1])];
    let windows = collect(&[(0, &app)], &[nodes]);
    let [window] = windows.as_slice() else {
        panic!("one window")
    };
    assert_eq!(window.info.title, "Doc");
    assert_eq!(window.info.app, "x");
    assert_eq!(window.info.process_instance.as_deref(), Some("boot:42"));
    assert_eq!(window.info.state.visible, Some(true));
    assert!(window.active);
    assert!(window.frame.global_position);
    assert_eq!(window.info.bounds.map(|rect| rect.width), Some(10.0));
}

#[test]
fn class_names_and_empty_titles_do_not_establish_window_purpose() {
    let app = app(Some(("gtk", "3.24")));
    for class in ["RootView", "MenuHostRootView", "arbitrary"] {
        let mut node = frame(23, "", "/org/a11y/atspi/accessible/2", &[25]);
        node.attributes = attributes(&[("class", class)]);
        assert_eq!(collect(&[(0, &app)], &[vec![node]]).len(), 1);
    }
    let nodes = vec![frame(75, "", "/org/a11y/atspi/accessible/3", &[25])];
    assert_eq!(collect(&[(0, &app)], &[nodes]).len(), 1);
}

#[test]
fn a_failed_state_read_leaves_visibility_unknown_and_never_active() {
    let app = app(None);
    let mut node = frame(23, "Doc", "/org/a11y/atspi/accessible/1", &[]);
    node.traits.states = Field::Failed("x".to_owned());
    node.extents = Field::Present(Extents {
        x: 0,
        y: 0,
        width: 5,
        height: 5,
    });
    let windows = collect(&[(0, &app)], &[vec![node]]);
    assert_eq!(windows[0].info.state.visible, None);
    assert!(!windows[0].active);
}

#[test]
fn a_frame_address_the_bus_could_not_have_issued_is_not_listed() {
    let app = app(None);
    let mut node = frame(23, "Doc", "/org/a11y/atspi/accessible/1", &[25]);
    node.element = AtspiElement::new("not-a-bus-name", "/p", 321);
    assert!(collect(&[(0, &app)], &[vec![node]]).is_empty());
}

#[test]
fn the_measured_about_and_shortcuts_shapes_are_listed_sheets() {
    let app = app(Some(("gtk", "4.14")));
    let nodes = vec![
        frame(23, "About", "/org/a11y/atspi/accessible/1", &[25, 16]),
        frame(16, "About Terminal", "/org/a11y/atspi/accessible/2", &[25]),
        frame(39, "", "/org/a11y/atspi/accessible/3", &[25, 16]),
        frame(39, "", "/org/a11y/atspi/accessible/4", &[25]),
        frame(23, "Doc", "/org/a11y/atspi/accessible/5", &[25]),
    ];
    let windows = collect(&[(0, &app)], &[nodes]);
    let sheets: Vec<bool> = windows.iter().map(|window| window.sheet).collect();
    assert_eq!(sheets, [true, true, true, false, false]);
}

#[test]
fn a_frame_that_neither_shows_nor_is_iconified_is_unshown_and_the_others_are_not() {
    let app = app(Some(("qt", "6.7")));
    let unshown = |bits: &[u32]| {
        let nodes = vec![frame(16, "d", "/org/a11y/atspi/accessible/1", bits)];
        collect(&[(0, &app)], &[nodes])[0].unshown
    };
    assert!(unshown(&[8, 24]));
    assert!(!unshown(&[8, 24, 25, 30]));
    assert!(!unshown(&[8, 15]));
}

#[test]
fn component_children_are_listed_with_their_dialog_and_sheet_evidence() {
    use crate::tree::vocabulary::state_bits::{ACTIVE, MODAL, SHOWING, VISIBLE};
    let app = app(Some(("gtk", "3.24")));
    let nodes = vec![
        frame(
            19,
            "Save As",
            "/org/a11y/atspi/accessible/1",
            &[ACTIVE, MODAL, SHOWING, VISIBLE],
        ),
        frame(2, "Alert", "/org/a11y/atspi/accessible/2", &[SHOWING]),
        frame(43, "Button", "/org/a11y/atspi/accessible/3", &[SHOWING]),
    ];
    let windows = collect(&[(0, &app)], &[nodes]);
    assert_eq!(windows.len(), 3);
    assert!(windows[0].dialog);
    assert!(windows[0].modal);
    assert!(windows[0].sheet);
    assert!(windows[0].active);
    assert!(windows[1].dialog);
    assert!(!windows[2].dialog);
    assert_eq!(windows[2].info.title, "Button");
    let mut absent = frame(
        23,
        "No Component",
        "/org/a11y/atspi/accessible/4",
        &[SHOWING],
    );
    absent.traits.interfaces = Field::Present(vec![]);
    assert!(collect(&[(0, &app)], &[vec![absent]]).is_empty());
}

#[test]
fn popup_menu_children_are_excluded_but_buttons_and_unread_children_stay_listed() {
    use crate::tree::vocabulary::state_bits::{ACTIVE, SHOWING, VISIBLE};
    let app = app(Some(("gtk", "3.24")));
    let mut nodes = vec![
        frame(69, "", "/gtk", &[ACTIVE, SHOWING, VISIBLE]),
        frame(23, "", "/chromium", &[SHOWING]),
        frame(69, "", "/button", &[SHOWING]),
        frame(69, "", "/failed", &[SHOWING]),
        frame(23, "Document", "/named", &[SHOWING]),
        frame(69, "", "/hidden", &[]),
    ];
    let mut reads = Vec::new();
    exclude_popup_menus(&mut nodes, |node| {
        reads.push(node.element.path().to_owned());
        match node.element.path() {
            "/gtk" => Some(vec![(33, vec![])]),
            "/chromium" => Some(vec![(34, vec![33])]),
            "/button" => Some(vec![(43, vec![])]),
            "/failed" => None,
            _ => panic!("only blank showing candidates may read children"),
        }
    });
    assert_eq!(reads, ["/gtk", "/chromium", "/button", "/failed"]);
    assert_eq!(
        nodes
            .iter()
            .map(|node| node.element.path())
            .collect::<Vec<_>>(),
        ["/button", "/failed", "/named", "/hidden"]
    );
    let windows = collect(&[(0, &app)], &[nodes]);
    assert_eq!(windows.len(), 4);
    assert_eq!(windows[0].info.title, "");
    assert_eq!(windows[1].info.state.visible, Some(true));
}

struct MenuNode {
    role: u32,
    children: Vec<&'static str>,
}

#[zbus::interface(name = "org.a11y.atspi.Accessible")]
impl MenuNode {
    fn get_role(&self) -> u32 {
        self.role
    }

    fn get_children(&self) -> Vec<(String, zbus::zvariant::OwnedObjectPath)> {
        self.children
            .iter()
            .map(|path| {
                (
                    "org.agentdesktop.WrapperMenu".to_owned(),
                    (*path).try_into().expect("fixture path"),
                )
            })
            .collect()
    }
}

#[test]
fn window_popup_classification_reads_wrappers_and_refuses_unread_or_cyclic_layouts() {
    use zbus::connection::Builder;
    for (leaf, children, expected) in [
        (33, vec![], true),
        (43, vec![], false),
        (20, vec!["/leaf"], false),
        (39, vec!["/missing"], false),
    ] {
        let (near, far) = std::os::unix::net::UnixStream::pair().unwrap();
        let server = Builder::async_io_unix_stream(far)
            .server(zbus::Guid::generate())
            .unwrap()
            .p2p()
            .serve_at(
                "/root",
                MenuNode {
                    role: 23,
                    children: vec!["/bar"],
                },
            )
            .unwrap()
            .serve_at(
                "/bar",
                MenuNode {
                    role: 34,
                    children: vec!["/panel"],
                },
            )
            .unwrap()
            .serve_at(
                "/panel",
                MenuNode {
                    role: 39,
                    children: vec!["/empty", "/filler"],
                },
            )
            .unwrap()
            .serve_at(
                "/empty",
                MenuNode {
                    role: 39,
                    children: vec![],
                },
            )
            .unwrap()
            .serve_at(
                "/filler",
                MenuNode {
                    role: 20,
                    children: vec!["/leaf"],
                },
            )
            .unwrap()
            .serve_at(
                "/leaf",
                MenuNode {
                    role: leaf,
                    children,
                },
            )
            .unwrap();
        let (client, _server) = zbus::block_on(futures_lite::future::zip(
            Builder::async_io_unix_stream(near).p2p().build(),
            server.build(),
        ));
        let client = client.unwrap();
        let _server = _server.unwrap();
        let mut node = frame(23, "", "/root", &[25]);
        node.element = AtspiElement::new("org.agentdesktop.WrapperMenu", "/root", 1);
        let children = zbus::block_on(popup_children(&client, &node));
        assert_eq!(is_popup_menu_surface(&node, children.as_deref()), expected);
        let mut budget = 0;
        assert!(zbus::block_on(popup_level(&client, &node.element, &mut budget)).is_none());
    }
}
