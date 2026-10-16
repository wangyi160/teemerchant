//! In-process C ABI used by teemerchant's private Python management engine.
//! No proxy process, local listener, user model APIs or unauthenticated fallback.
mod client;
mod merchant;
mod protocol;
#[cfg(feature = "snp-verifier")]
mod snp;
#[cfg(feature = "tdx-verifier")]
mod tdx;

use serde_json::{Value, json};
use std::{
    collections::HashMap,
    ffi::{CString, c_char},
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

const MAX_INPUT: usize = 20 * 1024 * 1024;
struct Client {
    runtime: tokio::runtime::Runtime,
    merchant: merchant::Merchant,
}
type ClientRef = Arc<Mutex<Client>>;
static CLIENTS: OnceLock<Mutex<HashMap<u64, ClientRef>>> = OnceLock::new();
static NEXT: AtomicU64 = AtomicU64::new(1);
fn clients() -> &'static Mutex<HashMap<u64, ClientRef>> {
    CLIENTS.get_or_init(Default::default)
}

fn output(action: impl FnOnce() -> Value) -> *mut c_char {
    let value = catch_unwind(AssertUnwindSafe(action))
        .unwrap_or_else(|_| json!({"error":"native_internal_error"}));
    CString::new(value.to_string())
        .expect("JSON escapes NUL")
        .into_raw()
}

unsafe fn input<'a>(data: *const u8, length: usize) -> Option<&'a [u8]> {
    if data.is_null() || length == 0 || length > MAX_INPUT {
        return None;
    }
    Some(unsafe { std::slice::from_raw_parts(data, length) })
}

#[unsafe(no_mangle)]
pub extern "C" fn tm_abi_version() -> u32 {
    1
}

/// # Safety
/// `data` must point to `length` readable bytes for the duration of this call.
/// Free the returned JSON string exactly once using `tm_free_response`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn tm_create(data: *const u8, length: usize) -> *mut c_char {
    output(|| {
        let result: anyhow::Result<u64> = (|| {
            let raw =
                unsafe { input(data, length) }.ok_or_else(|| anyhow::anyhow!("Invalid input"))?;
            let config: merchant::Config = serde_json::from_slice(raw)?;
            let runtime = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()?;
            let merchant = {
                let _entered = runtime.enter();
                merchant::Merchant::new(config)?
            };
            let handle = NEXT.fetch_add(1, Ordering::Relaxed);
            anyhow::ensure!(handle != 0, "Handle limit reached");
            clients()
                .lock()
                .map_err(|_| anyhow::anyhow!("Poisoned registry"))?
                .insert(handle, Arc::new(Mutex::new(Client { runtime, merchant })));
            Ok(handle)
        })();
        match result {
            Ok(handle) => json!({"handle":handle}),
            Err(_) => json!({"error":"invalid_router_or_trust_policy"}),
        }
    })
}

/// # Safety
/// `data` must point to `length` readable bytes until return. The caller owns
/// the returned JSON and must release it with `tm_free_response`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn tm_request(handle: u64, data: *const u8, length: usize) -> *mut c_char {
    output(|| {
        let Some(raw) = (unsafe { input(data, length) }) else {
            return json!({"error":"invalid_request"});
        };
        let Ok(request) = serde_json::from_slice::<merchant::Request>(raw) else {
            return json!({"error":"invalid_request"});
        };
        let context = clients()
            .lock()
            .ok()
            .and_then(|map| map.get(&handle).cloned());
        let Some(context) = context else {
            return json!({"error":"invalid_session"});
        };
        let Ok(mut context) = context.lock() else {
            return json!({"error":"native_internal_error"});
        };
        let Client { runtime, merchant } = &mut *context;
        match runtime.block_on(async {
            tokio::time::timeout(Duration::from_secs(120), merchant.request(request)).await
        }) {
            Ok(result) => result,
            Err(_) => {
                merchant.invalidate();
                json!({"error":"merchant_request_timeout","retry":"inspect_before_retrying_mutations"})
            }
        }
    })
}

#[unsafe(no_mangle)]
pub extern "C" fn tm_close(handle: u64) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        let context = clients()
            .lock()
            .ok()
            .and_then(|mut map| map.remove(&handle));
        drop(context);
    }));
}

/// # Safety
/// `value` must be NULL or a live string returned by this library, freed once.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn tm_free_response(value: *mut c_char) {
    if !value.is_null() {
        drop(unsafe { CString::from_raw(value) });
    }
}

#[cfg(test)]
mod tests;
