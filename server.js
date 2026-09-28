const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PORT || 3000;
const ROOT = __dirname;

const players = [
  { name: 'GitHub Copilot', elo: 2000, wins: 42, losses: 8 },
  { name: 'Marta', elo: 1925, wins: 35, losses: 11 },
  { name: 'Carlos', elo: 1850, wins: 31, losses: 12 },
  { name: 'Sofía', elo: 1820, wins: 30, losses: 14 },
  { name: 'Javier', elo: 1785, wins: 28, losses: 15 },
  { name: 'Ana', elo: 1710, wins: 26, losses: 18 },
  { name: 'Laura', elo: 1660, wins: 22, losses: 19 },
  { name: 'Luis', elo: 1600, wins: 20, losses: 21 },
  { name: 'Pepe', elo: 1490, wins: 17, losses: 24 }
];

function sendJson(res, payload, statusCode = 200) {
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*'
  });
  res.end(JSON.stringify(payload, null, 2));
}

function getMimeType(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  const mimeTypes = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon'
  };

  return mimeTypes[ext] || 'text/plain; charset=utf-8';
}

function serveStatic(res, filePath) {
  const safePath = path.normalize(filePath).replace(/^\.(?:\/|\\)?/, '');
  const absolutePath = path.join(ROOT, safePath);

  fs.readFile(absolutePath, (err, content) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('404 - Not found');
      return;
    }

    res.writeHead(200, { 'Content-Type': getMimeType(absolutePath) });
    res.end(content);
  });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (url.pathname === '/api/players') {
    sendJson(res, { players });
    return;
  }

  if (url.pathname === '/') {
    serveStatic(res, 'index.html');
    return;
  }

  const normalizedPath = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\//, '');
  serveStatic(res, normalizedPath);
});

server.listen(PORT, () => {
  console.log(`Servidor escuchando en http://localhost:${PORT}`);
});
