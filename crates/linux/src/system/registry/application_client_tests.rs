use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

use super::*;

struct Application {
    active: Arc<AtomicBool>,
    refuse: bool,
    silent: bool,
}

#[zbus::interface(name = "org.a11y.atspi.Application")]
impl Application {
    async fn get_application_bus_address(&self) -> zbus::fdo::Result<String> {
        if self.silent {
            std::future::pending::<()>().await;
        }
        if self.refuse {
            return Err(zbus::fdo::Error::AccessDenied(
                "registration refused".into(),
            ));
        }
        self.active.store(true, Ordering::SeqCst);
        Ok("unix:path=/must-not-be-opened".into())
    }
}

struct Accessible(Arc<AtomicBool>);

#[zbus::interface(name = "org.a11y.atspi.Accessible")]
impl Accessible {
    #[zbus(property)]
    fn name(&self) -> &str {
        if self.0.load(Ordering::SeqCst) {
            "active"
        } else {
            "inactive"
        }
    }
}

fn read_name(supported: bool, refuse: bool, silent: bool) -> Option<Option<String>> {
    let active = Arc::new(AtomicBool::new(false));
    let (client, server) = std::os::unix::net::UnixStream::pair().unwrap();
    let mut builder = zbus::connection::Builder::async_io_unix_stream(server)
        .server(zbus::Guid::generate())
        .unwrap()
        .p2p()
        .serve_at("/root", Accessible(active.clone()))
        .unwrap();
    if supported {
        builder = builder
            .serve_at(
                "/root",
                Application {
                    active,
                    refuse,
                    silent,
                },
            )
            .unwrap();
    }
    let (client, server) = zbus::block_on(futures_lite::future::zip(
        zbus::connection::Builder::async_io_unix_stream(client)
            .p2p()
            .build(),
        builder.build(),
    ));
    let client = client.unwrap();
    let _server = server.unwrap();
    zbus::block_on(within_slice(
        ROOT_SLICE,
        root_name(&client, &AtspiElement::new(":1.9", "/root", 1)),
    ))
    .flatten()
}

#[test]
fn application_client_is_registered_before_reading_root_metadata() {
    assert_eq!(read_name(true, false, false), Some(Some("active".into())));
}

#[test]
fn a_refused_application_registration_does_not_authorize_a_root_read() {
    assert_eq!(read_name(true, true, false), None);
}

#[test]
fn a_toolkit_without_the_optional_application_method_keeps_its_observation() {
    assert_eq!(
        read_name(false, false, false),
        Some(Some("inactive".into()))
    );
}

#[test]
fn a_silent_application_registration_stays_within_the_root_observation_budget() {
    let started = std::time::Instant::now();
    assert_eq!(read_name(true, false, true), None);
    assert!(started.elapsed() < Duration::from_secs(1));
}
