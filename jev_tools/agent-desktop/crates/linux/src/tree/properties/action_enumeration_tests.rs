use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use zbus::{Connection, connection::Builder};

use super::{ACTION_INTERFACE, MAX_ACTIONS, read_actions};
use crate::tree::properties::liveness::Liveness;
use crate::tree::{AtspiElement, Field};

#[derive(Default)]
struct Calls {
    bulk: AtomicUsize,
    counts: AtomicUsize,
    names: Mutex<Vec<i32>>,
    invoked: Mutex<Vec<i32>>,
}

struct ActionFixture {
    count: i32,
    names: Vec<&'static str>,
    failed_index: Option<i32>,
    unreadable_count: bool,
    calls: Arc<Calls>,
}

impl ActionFixture {
    fn new(names: Vec<&'static str>) -> Self {
        Self {
            count: names.len() as i32,
            names,
            failed_index: None,
            unreadable_count: false,
            calls: Arc::new(Calls::default()),
        }
    }
}

#[zbus::interface(name = "org.a11y.atspi.Action")]
impl ActionFixture {
    #[zbus(property, name = "NActions")]
    fn n_actions(&self) -> zbus::fdo::Result<i32> {
        self.calls.counts.fetch_add(1, Ordering::SeqCst);
        if self.unreadable_count {
            Err(zbus::fdo::Error::UnknownProperty("NActions".into()))
        } else {
            Ok(self.count)
        }
    }

    fn get_name(&self, index: i32) -> zbus::fdo::Result<&str> {
        self.calls.names.lock().unwrap().push(index);
        if self.failed_index == Some(index) {
            Err(zbus::fdo::Error::Failed("unreadable action".into()))
        } else {
            self.names
                .get(index as usize)
                .copied()
                .ok_or_else(|| zbus::fdo::Error::InvalidArgs("action index".into()))
        }
    }

    fn get_actions(&self) -> zbus::fdo::Result<Vec<(String, String, String)>> {
        self.calls.bulk.fetch_add(1, Ordering::SeqCst);
        Err(zbus::fdo::Error::Failed("unsafe legacy method".into()))
    }

    fn do_action(&self, index: i32) -> bool {
        self.calls.invoked.lock().unwrap().push(index);
        (0..self.count).contains(&index)
    }
}

fn connect(service: impl zbus::object_server::Interface) -> (Connection, Connection) {
    let (near, far) = std::os::unix::net::UnixStream::pair().unwrap();
    let (client, server) = zbus::block_on(futures_lite::future::zip(
        Builder::async_io_unix_stream(near).p2p().build(),
        Builder::async_io_unix_stream(far)
            .server(zbus::Guid::generate())
            .unwrap()
            .p2p()
            .serve_at("/button", service)
            .unwrap()
            .build(),
    ));
    (client.unwrap(), server.unwrap())
}

fn element() -> AtspiElement {
    AtspiElement::new(":1.1", "/button", 1)
}

fn read(client: &Connection) -> Field<Vec<String>> {
    zbus::block_on(read_actions(
        client,
        &element(),
        &Field::Present(vec![ACTION_INTERFACE.into()]),
        Liveness::Alive,
    ))
    .unwrap()
}

#[test]
fn indexed_action_enumeration_preserves_the_index_delivered_to_do_action() {
    let fixture = ActionFixture::new(vec!["", "showContextMenu", "click"]);
    let calls = fixture.calls.clone();
    let (client, _server) = connect(fixture);
    let names = read(&client).present().unwrap().clone();
    assert_eq!(names, ["", "showContextMenu", "click"]);
    let index = crate::actions::action_call::resolve_verb(&names, &["click"]).unwrap();
    let reply = zbus::block_on(super::call(
        &client,
        &element(),
        ACTION_INTERFACE,
        "DoAction",
        &(index,),
    ))
    .unwrap();
    assert!(reply.body().deserialize::<bool>().unwrap());
    assert_eq!(*calls.invoked.lock().unwrap(), [2]);
    assert_eq!(calls.counts.load(Ordering::SeqCst), 1);
    let mut indices = calls.names.lock().unwrap().clone();
    indices.sort();
    assert_eq!(indices, [0, 1, 2]);
    assert_eq!(calls.bulk.load(Ordering::SeqCst), 0);
}

#[test]
fn a_confirmed_zero_action_count_is_an_empty_list() {
    let fixture = ActionFixture::new(vec![]);
    let calls = fixture.calls.clone();
    let (client, _server) = connect(fixture);
    assert_eq!(read(&client), Field::Present(vec![]));
    assert!(calls.names.lock().unwrap().is_empty());
    assert_eq!(calls.bulk.load(Ordering::SeqCst), 0);
}

#[test]
fn a_missing_count_is_unknown_without_a_bulk_fallback() {
    let mut fixture = ActionFixture::new(vec!["click"]);
    fixture.unreadable_count = true;
    let calls = fixture.calls.clone();
    let (client, _server) = connect(fixture);
    assert!(read(&client).is_failed());
    assert!(calls.names.lock().unwrap().is_empty());
    assert_eq!(calls.bulk.load(Ordering::SeqCst), 0);
}

#[test]
fn an_unreadable_name_keeps_actions_unknown_without_shifting_later_indices() {
    let mut fixture = ActionFixture::new(vec!["showContextMenu", "click"]);
    fixture.failed_index = Some(0);
    let calls = fixture.calls.clone();
    let (client, _server) = connect(fixture);
    let Field::Failed(detail) = read(&client) else {
        panic!("an unreadable index must not become a shorter action list");
    };
    assert!(detail.contains("GetName(0)"));
    assert!(calls.invoked.lock().unwrap().is_empty());
    assert_eq!(calls.bulk.load(Ordering::SeqCst), 0);
}

#[test]
fn negative_and_excessive_action_counts_are_unknown_before_enumeration() {
    for count in [-1, MAX_ACTIONS + 1, i32::MAX] {
        let mut fixture = ActionFixture::new(vec![]);
        fixture.count = count;
        let calls = fixture.calls.clone();
        let (client, _server) = connect(fixture);
        assert!(read(&client).is_failed());
        assert!(calls.names.lock().unwrap().is_empty());
        assert_eq!(calls.bulk.load(Ordering::SeqCst), 0);
    }
}

#[test]
fn the_maximum_supported_action_count_is_read_without_truncation() {
    let fixture = ActionFixture::new(vec!["click"; MAX_ACTIONS as usize]);
    let calls = fixture.calls.clone();
    let (client, _server) = connect(fixture);
    assert_eq!(read(&client).present().unwrap().len(), MAX_ACTIONS as usize);
    assert_eq!(calls.names.lock().unwrap().len(), MAX_ACTIONS as usize);
    assert_eq!(calls.bulk.load(Ordering::SeqCst), 0);
}

struct MalformedActions;

#[zbus::interface(name = "org.a11y.atspi.Action")]
impl MalformedActions {
    #[zbus(property, name = "NActions")]
    fn n_actions(&self) -> &str {
        "1"
    }
}

#[test]
fn a_malformed_count_is_unknown_rather_than_coerced() {
    let (client, _server) = connect(MalformedActions);
    assert!(read(&client).is_failed());
}

#[test]
fn action_count_transport_loss_ends_observation() {
    let client = crate::tree::live::test_support::closed_connection();
    let error = zbus::block_on(read_actions(
        &client,
        &element(),
        &Field::Present(vec![ACTION_INTERFACE.into()]),
        Liveness::Alive,
    ))
    .expect_err("a disconnected bus cannot provide action evidence");
    assert_eq!(error.code, agent_desktop_core::ErrorCode::Internal);
}
