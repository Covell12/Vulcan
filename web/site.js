// Presentation-only enhancements for the marketing chrome (nav, hero, sections).
// Everything here is progressive: if an element is missing, or JS is disabled,
// the site still works — the app flow lives in intents.js/app.js and doesn't
// depend on any of this.
(function () {
  const nav = document.getElementById("site-nav");
  const toggle = document.getElementById("nav-toggle");
  const links = document.getElementById("nav-links");

  // Solidify the nav background once the page scrolls past the hero top.
  if (nav) {
    const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  // Mobile menu.
  if (toggle && nav) {
    toggle.addEventListener("click", () => {
      const open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  if (links && nav) {
    links.addEventListener("click", (e) => {
      if (e.target.tagName === "A") {
        nav.classList.remove("open");
        if (toggle) toggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  // Reveal-on-scroll. If IntersectionObserver is unavailable, just show them.
  const reveals = document.querySelectorAll(".reveal");
  if (reveals.length) {
    if ("IntersectionObserver" in window) {
      const io = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (entry.isIntersecting) {
              entry.target.classList.add("in");
              io.unobserve(entry.target);
            }
          }
        },
        { rootMargin: "0px 0px -10% 0px" }
      );
      reveals.forEach((el) => io.observe(el));
    } else {
      reveals.forEach((el) => el.classList.add("in"));
    }
  }
})();
