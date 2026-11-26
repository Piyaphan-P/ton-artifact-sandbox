const http = require('node:http');
http.createServer((req,res) => {
  res.setHeader('Content-Type','application/json');
  res.end(JSON.stringify({message:'Dynamic artifact is running',runtime:process.version,time:new Date().toISOString(),secretConfigured:Boolean(process.env.DEMO_SECRET)}));
}).listen(Number(process.env.PORT||3000),'0.0.0.0');
