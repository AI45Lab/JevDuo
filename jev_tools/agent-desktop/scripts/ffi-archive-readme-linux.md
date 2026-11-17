
Linux needs glibc 2.35 or newer.

`dlopen` and Python `ctypes` callers load `lib/libagent_desktop_ffi.so` by path. The library has no SONAME, so a program that links it records the file name. Pass `-L` for the `lib/` directory at link time, and `-rpath` pointing at it at run time.

For the full API, see https://agent-browser.dev/desktop/ffi.

- Mutations run one at a time across threads and processes, under an interaction lock. Reads can overlap them, and many threads can share one adapter. Resolve, use and free a native handle on one thread, with the adapter that created it. See https://agent-browser.dev/desktop/ffi/ownership-and-threading.
- `ad_snapshot` takes an application name. It has no window id or root element argument.
- The clipboard calls carry text only. `ad_set_clipboard` starts the detached `lib/agent-desktop-linux-clipboard-owner` process. The process keeps serving the text after the host exits, until another client takes the selection or `ad_clear_clipboard` ends it. The library finds the helper beside its own image, never through `PATH`, so keep both files in the same directory. Wayland needs `wl-clipboard` installed.
- Observation needs a session accessibility bus (`at-spi2-core`). Action enumeration uses `NActions` and `GetName(i)` to avoid the `GetActions` crash in older ATK bridges shipped with Ubuntu 22.04. Ubuntu 24.04+ remains the primary validation target; full desktop support on 22.04 is not established.
