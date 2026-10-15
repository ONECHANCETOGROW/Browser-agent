// One job: trigger Chrome's microphone permission prompt in a normal tab (a side panel may not be able to show it), then release the microphone at once.
// No audio is read, recorded or kept: the stream is stopped immediately.
(() => {
  const msg = document.getElementById('msg');
  document.getElementById('allow').addEventListener('click', async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      for (const t of stream.getTracks()) t.stop();
      msg.textContent = 'Microphone allowed. You can close this tab and click the microphone button in the side panel.';
    } catch (e) {
      msg.textContent = e && e.name === 'NotAllowedError'
        ? 'Microphone access was blocked. Click the lock/tune icon in the address bar, allow the microphone, then try again.'
        : 'No microphone could be opened (' + ((e && e.name) || 'unknown error') + ').';
    }
  });
})();
