/* Applies a saved light/dark preference before first paint (classic script, no module). */
(function () {
  try {
    var t = window.localStorage.getItem('heyp-theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) {
    /* storage unavailable (private mode, blocked site data) — follow the system theme */
  }
})();
