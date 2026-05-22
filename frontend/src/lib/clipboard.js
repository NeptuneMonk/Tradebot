/**
 * Robust clipboard copy that works inside restricted iframes (Emergent preview)
 * where the Clipboard API is blocked by permissions policy.
 *
 * Try modern API first → fall back to a hidden textarea + execCommand('copy').
 * Returns true if successful.
 */
export async function copyToClipboard(text) {
  if (!text) return false;
  // Try modern Clipboard API
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {
    // fall through to legacy
  }
  // Legacy fallback: hidden textarea + execCommand
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    ta.style.left = "-9999px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}
