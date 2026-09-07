// Converts mono Float32 mic audio to 16-bit little-endian PCM.
//
// The AudioContext is created at the target sample rate, so no resampling is
// needed here -- the browser's own resampler does it upstream, and this worklet
// stays cheap enough to run on a phone.
class PcmWorklet extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0];
    if (!channel) return true;

    const out = new Int16Array(channel.length);
    for (let i = 0; i < channel.length; i++) {
      const s = Math.max(-1, Math.min(1, channel[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.port.postMessage(out.buffer, [out.buffer]);
    return true;
  }
}

registerProcessor('pcm-worklet', PcmWorklet);
