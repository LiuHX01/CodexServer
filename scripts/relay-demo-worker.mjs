import { hostname, networkInterfaces } from 'node:os';

const token = process.argv[2];
const endpoint = process.argv[3] || 'wss://merchant-changeatlas-prt.test.gifshow.com/api/v1/relay-demo/worker';
const expectedIp = '172.24.4.55';
if (!token || typeof WebSocket === 'undefined') throw new Error('需要 Node.js 22+ 和本次演示配对码。');
if (!endpoint.startsWith('wss://')) throw new Error('需要 WSS 加密连接。');
const identity = { hostname: hostname(), localAddressMatchesTarget: Object.values(networkInterfaces()).flat().some(address => address?.address === expectedIp) };
if (!identity.localAddressMatchesTarget) throw new Error(`请在 ${expectedIp} 上运行本程序。`);
let socket, reconnect, stopped = false, attempts = 0;
function connect() {
  socket = new WebSocket(endpoint);
  const opening = setTimeout(() => socket.close(), 10000);
  socket.onopen = () => socket.send(JSON.stringify({ type: 'hello', token, ...identity }));
  socket.onerror = () => console.error('连接失败，检查公司网络或配对码。');
  socket.onmessage = ({ data }) => {
    let message;
    try { message = JSON.parse(data); } catch { return; }
    if (message.type === 'ready') {
      clearTimeout(opening);
      attempts = 0;
      console.log(JSON.stringify({ event: 'worker_ready', ...identity, expectedIp, expiresAt: new Date(message.expiresAt).toISOString() }));
      console.log('保持终端开启。现在可以在测试群 @ChangeAtlas变更智图 你好');
    } else if (message.type === 'task' && message.operation === 'echo' && typeof message.text === 'string' && message.text.length <= 200) {
      console.log(JSON.stringify({ event: 'task_received', taskId: message.taskId, text: message.text }));
      socket.send(JSON.stringify({ type: 'result', taskId: message.taskId, nonce: message.nonce, text: message.text }));
    } else if (message.type === 'ack') {
      console.log(JSON.stringify({ event: 'backend_ack', taskId: message.taskId, roundTripMs: message.roundTripMs }));
    }
  };
  socket.onclose = ({ code }) => {
    clearTimeout(opening);
    if ([4001, 4003, 4009].includes(code)) {
      stopped = true;
      console.error(`连接结束（${code}）：配对已过期、被替换或已有电脑连接。`);
    }
    if (!stopped) {
      const delay = Math.min(1000 * 2 ** attempts++, 15000);
      console.log(JSON.stringify({ event: 'reconnecting', delayMs: delay }));
      reconnect = setTimeout(connect, delay);
    }
  };
}
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => { stopped = true; clearTimeout(reconnect); socket?.close(); });
connect();