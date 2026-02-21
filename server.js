const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 7890;
const WS_ROOT = '/data/workspace';

function readJSON(file) {
  try { return JSON.parse(fs.readFileSync(path.join(WS_ROOT, file), 'utf8')); }
  catch { return null; }
}

function getPositions() {
  const positions = (readJSON('active-positions.json') || []).map(p => {
    const dsl = readJSON(`dsl-state-${p.asset}.json`);
    return { ...p, dsl: dsl || {} };
  });
  const strategy = readJSON('auto-strategy.json') || {};
  const history = readJSON('trade-history.json') || [];
  return { positions, strategy, history };
}

const server = http.createServer((req, res) => {
  if (req.url === '/api/positions') {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
    res.end(JSON.stringify(getPositions()));
  } else if (req.url === '/' || req.url === '/index.html') {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8'));
  } else {
    res.writeHead(404);
    res.end('Not found');
  }
});

server.listen(PORT, '0.0.0.0', () => console.log(`Dashboard: http://localhost:${PORT}`));
