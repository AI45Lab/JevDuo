use super::{Node, top_level};
use crate::system::menu::menu_fixture::{
    BUTTON, FRAME, ITEM, MENU, MENU_BAR, PANEL, SHOWING, WINDOW, item, node,
};
use crate::tree::vocabulary::role_numbers::FILLER;

fn wrapped_menu(kind: u32, layers: usize) -> Node {
    let custom = node(
        ITEM,
        "Custom",
        &[SHOWING],
        vec![node(BUTTON, "Zoom", &[SHOWING], vec![])],
    );
    let mut content = node(MENU, "", &[SHOWING], vec![item("Open", true), custom]);
    for index in 0..layers {
        let wrapper = if index % 2 == 0 { PANEL } else { FILLER };
        content = node(
            wrapper,
            "",
            &[SHOWING],
            vec![node(PANEL, "", &[], vec![]), content],
        );
    }
    node(
        kind,
        "",
        &[SHOWING],
        vec![node(MENU_BAR, "", &[SHOWING], vec![content])],
    )
}

#[test]
fn menu_layout_wrappers_preserve_the_popup_and_its_custom_item_content() {
    for kind in [FRAME, WINDOW] {
        for layers in 0..5 {
            let tree = wrapped_menu(kind, layers);
            let popup = top_level(&tree).expect("layout containers preserve the menu");
            assert_eq!(popup.items, 2);
            assert_eq!(popup.root.payload.node.traits.role.present(), Some(&MENU));
        }
    }
}

#[test]
fn unread_wrappers_and_non_menu_siblings_cannot_establish_a_popup() {
    let mut tree = wrapped_menu(FRAME, 2);
    tree.children[0]
        .children
        .push(node(BUTTON, "Other action", &[SHOWING], vec![]));
    assert!(top_level(&tree).is_none());
    let mut tree = wrapped_menu(FRAME, 2);
    tree.children[0].children[0].truncated = true;
    assert!(top_level(&tree).is_none());
    tree.children[0].children[0].truncated = false;
    tree.children[0].children[0].payload.node.traits.role =
        crate::tree::Field::Failed("unread".into());
    assert!(top_level(&tree).is_none());
    let empty = node(
        FRAME,
        "",
        &[SHOWING],
        vec![node(PANEL, "", &[SHOWING], vec![])],
    );
    assert!(top_level(&empty).is_none());
}
#[test]
fn focus_belongs_to_its_showing_popup_and_not_to_an_ancestor_menu() {
    use super::owns_focus;
    use crate::system::menu::menu_fixture::{
        BUTTON, DOCUMENT_WEB, ITEM, MENU, PANEL, SHOWING, node,
    };
    use crate::tree::vocabulary::state_bits::FOCUSED;

    for role in [ITEM, BUTTON] {
        let menu = node(
            MENU,
            "Child",
            &[SHOWING],
            vec![node(
                PANEL,
                "",
                &[SHOWING],
                vec![node(role, "Entry", &[SHOWING, FOCUSED], vec![])],
            )],
        );
        assert!(owns_focus(&menu));
        let parent = node(MENU, "Parent", &[SHOWING], vec![menu]);
        assert!(!owns_focus(&parent));
    }
    for (role, states) in [
        (ITEM, vec![FOCUSED]),
        (DOCUMENT_WEB, vec![SHOWING, FOCUSED]),
    ] {
        let menu = node(
            MENU,
            "",
            &[SHOWING],
            vec![node(role, "Hidden or web", &states, vec![])],
        );
        assert!(!owns_focus(&menu));
    }
}
