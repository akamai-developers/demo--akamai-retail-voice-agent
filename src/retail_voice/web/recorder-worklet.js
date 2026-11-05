// Posts each 128-frame block with its RMS level, so the page can draw a level meter and
// detect silence without a second pass over the samples. The block's buffer is transferred.
class Recorder extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) {
      const samples = channel.slice();
      let sum = 0;
      for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
      const rms = samples.length ? Math.sqrt(sum / samples.length) : 0;
      this.port.postMessage({ samples, rms }, [samples.buffer]);
    }
    return true;
  }
}
registerProcessor('pcm-recorder', Recorder);
