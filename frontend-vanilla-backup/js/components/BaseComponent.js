/**
 * 🧱 BaseComponent.js
 * A minimal base class for Reactive Classes.
 */

export class BaseComponent {
    constructor() {
        this._state = {};
    }

    // Utility to subscribe to AppState if available
    subscribe(store) {
        if (store && typeof store.subscribe === 'function') {
            store.subscribe((prop, val) => {
                this.onStateChange(prop, val);
            });
        }
    }

    onStateChange(_prop, _val) {
        // To be overridden by subclasses
    }

    render() {
        // To be overridden by subclasses
    }
}
