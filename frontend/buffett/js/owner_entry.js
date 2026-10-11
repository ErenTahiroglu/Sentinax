// Validation of the public "Sahip Girişi" destination (Phase 25D1E4). Pure: the only inputs are the pinned config and the page's own origin.
// The link is a plain navigation to a SEPARATE origin: nothing is transferred, no message is posted, no form exists, and the visitor cannot choose the target.
(function (root) {
    'use strict';
    var APPROVED_PATH = '/owner/';
    var HTTPS_ORIGIN = /^https:\/\/(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$/;
    var LOOPBACK_ORIGIN = /^http:\/\/(?:127\.0\.0\.1|localhost):([0-9]{2,5})$/;
    var LOOPBACK_HOSTS = ['127.0.0.1', 'localhost'];

    function resolve(cfg, loc) {
        if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg) || !loc) return null;
        var keys = Object.keys(cfg).sort();
        if (keys.length !== 2 || keys[0] !== 'origin' || keys[1] !== 'path') return null;
        if (cfg.path !== APPROVED_PATH || typeof cfg.origin !== 'string' || cfg.origin.length > 262) return null;
        var origin = cfg.origin;
        if (origin === loc.origin) return null; // the owner login must live on a different origin
        if (HTTPS_ORIGIN.test(origin)) return origin + APPROVED_PATH;
        // Offline harness only: a loopback http target is honoured when the public page itself is served from loopback over http.
        var m = LOOPBACK_ORIGIN.exec(origin);
        if (m && Number(m[1]) <= 65535 && loc.protocol === 'http:' && LOOPBACK_HOSTS.indexOf(loc.hostname) !== -1) return origin + APPROVED_PATH;
        return null;
    }

    root.ownerEntry = Object.freeze({ resolve: resolve });
}(window));
