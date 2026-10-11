// Phase 25D1E1: Deep Ocean private shell (OFFLINE prototype). Navigation and the simple/advanced mode live in memory only:
// no storage, no cookies, no network, no token handling, no login. A real authenticated transition needs its own reviewed design.
(function () {
    'use strict';
    var panels = Array.prototype.slice.call(document.querySelectorAll('[data-panel]'));
    var navButtons = Array.prototype.slice.call(document.querySelectorAll('[data-target]'));
    var modeButtons = Array.prototype.slice.call(document.querySelectorAll('[data-mode-set]'));

    function show(name) {
        panels.forEach(function (p) { p.hidden = p.getAttribute('data-panel') !== name; });
        navButtons.forEach(function (b) {
            if (b.getAttribute('data-target') === name) { b.setAttribute('aria-current', 'page'); } else { b.removeAttribute('aria-current'); }
        });
    }
    function setMode(mode) {
        document.body.setAttribute('data-mode', mode);
        modeButtons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-mode-set') === mode)); });
    }
    navButtons.forEach(function (b) { b.addEventListener('click', function () { show(b.getAttribute('data-target')); }); });
    modeButtons.forEach(function (b) { b.addEventListener('click', function () { setMode(b.getAttribute('data-mode-set')); }); });
    show('overview');
    setMode('simple');
}());
