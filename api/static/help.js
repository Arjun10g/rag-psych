// Lightweight entrance animations for the help page.
const { gsap } = window;

gsap.from("#help-hero", { opacity: 0, y: -12, duration: 0.6, ease: "power2.out" });
gsap.from("section", {
  opacity: 0, y: 16, duration: 0.5, stagger: 0.08, delay: 0.15, ease: "power2.out",
});
