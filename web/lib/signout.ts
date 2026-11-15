import { post, setCsrf } from "./api";
import { clearAllCachesSoon } from "./caches";

// Sign-out always completes: the server revoke is attempted, client state is dropped and the
// redirect happens whatever the network or the service worker do. Cache clearing runs alongside,
// best effort, and can hold the redirect up for half a second at most.
export async function signOut(redirect: () => void): Promise<void> {
  const clearing = clearAllCachesSoon();
  try {
    await post("/api/auth/logout");
  } catch {
    // offline or already signed out: the server session ends on its idle timeout
  }
  setCsrf(null);
  await clearing;
  redirect();
}
