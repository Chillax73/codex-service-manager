const http = require('http');
const port = Number(process.argv[2]);
http.createServer((req, res) => {
  res.writeHead(200);
  res.end('poc-node');
}).listen(port, '127.0.0.1');
