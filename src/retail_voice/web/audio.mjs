export function toWav(chunks, sampleRate) {
  const length = chunks.reduce((n, x) => n + x.length, 0);
  const source = new Float32Array(length);
  let offset = 0;
  for (const chunk of chunks) { source.set(chunk, offset); offset += chunk.length; }
  const count = Math.floor(length * 16000 / sampleRate);
  const buffer = new ArrayBuffer(44 + count * 2), view = new DataView(buffer);
  const chars = (offset, text) => [...text].forEach((c, i) => view.setUint8(offset+i, c.charCodeAt(0)));
  chars(0, 'RIFF'); view.setUint32(4, 36+count*2, true); chars(8, 'WAVE'); chars(12, 'fmt ');
  view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,16000,true); view.setUint32(28,32000,true); view.setUint16(32,2,true);
  view.setUint16(34,16,true); chars(36,'data'); view.setUint32(40,count*2,true);
  // Box-filter downsampling; adequate for this diagnostic, not a production resampler.
  for(let i=0;i<count;i++) {
    const start=Math.floor(i*sampleRate/16000), end=Math.max(start+1,Math.floor((i+1)*sampleRate/16000));
    let value=0; for(let j=start;j<end && j<length;j++) value+=source[j];
    value=Math.max(-1,Math.min(1,value/(end-start)));
    view.setInt16(44+i*2,value<0?value*32768:value*32767,true);
  }
  return new Blob([buffer],{type:'audio/wav'});
}
