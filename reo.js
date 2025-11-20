!function () {
    var clientId, onLoad, scriptEl;

    clientId = "aec8f1727a88835";

    onLoad = function () {
        try {
            if (typeof Reo !== "undefined" && Reo && typeof Reo.init === "function") {
                Reo.init({ clientID: clientId });
            }
        } catch (e) {
            // Silently ignore to avoid impacting docs rendering
        }
    };

    scriptEl = document.createElement("script");
    scriptEl.src = "https://static.reo.dev/" + clientId + "/reo.js";
    scriptEl.async = true;
    scriptEl.onload = onLoad;
    document.head.appendChild(scriptEl);
}();