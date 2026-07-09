// Fetch-based submission for audio-queue forms (single lesson and whole
// course), shared by the course and lesson detail pages. Results land in the
// page's flash banner without a navigation.
(() => {
  function showFlash(message, level) {
    const banner = document.querySelector("[data-flash-banner]");
    if (!banner) return;
    const heading = banner.querySelector("h3");
    const eyebrow = banner.querySelector(".eyebrow");
    if (heading) heading.textContent = message;
    if (eyebrow) eyebrow.textContent = level === "error" ? "Error" : "Status";
    banner.classList.toggle("flash-banner-error", level === "error");
    banner.classList.remove("hidden");
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-audio-queue-form], [data-audio-queue-all-form]");
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "application/json" },
        body: new FormData(form),
      });
      const payload = await response.json().catch(() => null);
      const message = payload?.message || payload?.detail || (response.ok ? "Request sent." : `Request failed (${response.status})`);
      const level = payload?.level || (response.ok ? "info" : "error");
      showFlash(message, level);
    } catch (error) {
      showFlash("Network error while queuing audio.", "error");
    } finally {
      if (button) button.disabled = false;
    }
  });
})();
