/**
 * absolute_term 弹窗封装：统一走 /shared/ui.js 的 UiWindow（可拖、可缩放、无遮罩）。
 * 以后凡是弹窗都用 AppDialog，不要再手写遮罩层。
 */
(function (global) {
  "use strict";

  function requireUiWindow() {
    if (typeof global.UiWindow !== "function") {
      throw new Error("缺少 /shared/ui.js 的 UiWindow，请检查 nginx /shared/");
    }
    return global.UiWindow;
  }

  class AppDialog {
    /**
     * @param {{
     *   title?: string,
     *   body?: string|HTMLElement,
     *   width?: number,
     *   height?: number,
     *   wide?: boolean,
     *   actions?: Array<{label:string, className?:string, onClick?:Function}>,
     *   onClose?: Function
     * }} opts
     */
    constructor(opts) {
      const UiWindow = requireUiWindow();
      this._win = new UiWindow(opts || {});
    }

    open() {
      this._win.open();
      return this;
    }

    close() {
      this._win.close();
      return this;
    }

    setBody(nodeOrText) {
      this._win.setBody(nodeOrText);
      return this;
    }

    setTitle(title) {
      if (this._win.titleEl) this._win.titleEl.textContent = title || "";
      return this;
    }

    /** 快捷打开；返回 AppDialog 实例 */
    static open(opts) {
      return new AppDialog(opts).open();
    }

    /** 确认框（复用 shared UiConfirm） */
    static confirm(opts) {
      if (typeof global.UiConfirm !== "function") {
        throw new Error("缺少 /shared/ui.js 的 UiConfirm");
      }
      return global.UiConfirm(opts);
    }
  }

  global.AppDialog = AppDialog;
})(typeof window !== "undefined" ? window : globalThis);
