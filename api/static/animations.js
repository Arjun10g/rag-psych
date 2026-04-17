// GSAP-powered interactions:
//   1. Hero fade-in on initial page load
//   2. Results card + chunk-card stagger on every HTMX swap
//   3. Citation hover/focus → glow + scale the matching chunk card and
//      smooth-scroll it into view; a click locks the highlight briefly

const { gsap } = window;
const LOCK_MS = 1400;

// ─── 1. Page-load entrance ────────────────────────────────────────────────
gsap.from("#hero", { opacity: 0, y: -16, duration: 0.8, ease: "power2.out" });
gsap.from("#search-form", { opacity: 0, y: 20, duration: 0.7, delay: 0.2, ease: "power2.out" });

// ─── 2. Results swap-in animation ─────────────────────────────────────────
document.body.addEventListener("htmx:afterSwap", (e) => {
  if (e.detail.target.id !== "results") return;
  const shell = e.detail.target.querySelector(".results-shell");
  if (!shell) return;

  gsap.from(shell, { opacity: 0, y: 20, duration: 0.5, ease: "power2.out" });
  gsap.from(shell.querySelectorAll(".chunk-card"), {
    opacity: 0, y: 14, duration: 0.45, stagger: 0.06, delay: 0.15, ease: "power2.out",
  });
  hookCitations(shell);
});

// ─── 3. Citation ↔ chunk-card linking ─────────────────────────────────────
function hookCitations(root) {
  const cards = new Map();
  root.querySelectorAll(".chunk-card").forEach((c) => {
    cards.set(c.dataset.chunkId, c);
  });

  root.querySelectorAll(".citation").forEach((cite) => {
    const cid = cite.dataset.chunk;
    const target = cards.get(cid);
    if (!target) {
      // Cited ID not in retrieved set — flag as a hallucinated citation.
      cite.classList.add("citation-invalid");
      return;
    }
    let lockTimer = null;
    const highlight = (lock = false) => {
      gsap.to(target, { scale: 1.03, duration: 0.25, ease: "power2.out", overwrite: true });
      target.classList.add("chunk-glow");
      cite.classList.add("citation-active");
      if (lock) {
        clearTimeout(lockTimer);
        lockTimer = setTimeout(() => unhighlight(), LOCK_MS);
      }
    };
    const unhighlight = () => {
      gsap.to(target, { scale: 1, duration: 0.25, ease: "power2.out", overwrite: true });
      target.classList.remove("chunk-glow");
      cite.classList.remove("citation-active");
    };
    cite.addEventListener("mouseenter", () => highlight(false));
    cite.addEventListener("mouseleave", () => { if (!lockTimer) unhighlight(); });
    cite.addEventListener("focus", () => highlight(false));
    cite.addEventListener("blur", () => { if (!lockTimer) unhighlight(); });
    cite.addEventListener("click", (ev) => {
      ev.preventDefault();
      highlight(true);
      gsap.to(window, {
        duration: 0.6,
        scrollTo: { y: target, offsetY: 80 },
        ease: "power2.inOut",
      });
    });
  });
}
