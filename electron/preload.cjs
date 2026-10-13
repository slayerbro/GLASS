// Narrow, whitelisted bridge between renderer pages and the main process.
const { contextBridge, ipcRenderer } = require('electron');

const SEND = new Set(['overlay:resize', 'overlay:focus', 'overlay:ignore-mouse', 'overlay:hide', 'dashboard:open',
  'snip:start', 'snip:cancel', 'snip:done', 'snip:vision', 'snip:frozen', 'app:quit']);
const INVOKE = new Set(['engine:config', 'engine:log', 'engine:restart', 'engine:setup', 'ui:get', 'ui:set', 'export:markdown', 'export:pdf']);
const ON = new Set(['engine:state', 'shortcut', 'snip:rect', 'snip:vision', 'snip:freeze', 'snip:image', 'overlay:bounds', 'dashboard:view', 'ui:changed']);

contextBridge.exposeInMainWorld('glass', {
  platform: process.platform,
  send: (ch, data) => { if (SEND.has(ch)) ipcRenderer.send(ch, data); },
  invoke: (ch, data) => (INVOKE.has(ch) ? ipcRenderer.invoke(ch, data) : Promise.reject(new Error('blocked channel'))),
  on: (ch, cb) => {
    if (!ON.has(ch)) return () => {};
    const f = (_e, d) => cb(d);
    ipcRenderer.on(ch, f);
    return () => ipcRenderer.removeListener(ch, f);
  },
});
