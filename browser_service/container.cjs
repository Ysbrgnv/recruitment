'use strict';
const fs=require('node:fs');
const key=fs.readFileSync('/run/secrets/browser_key','utf8').trim();
if(!/^[a-f0-9]{64}$/.test(key))throw Error('Invalid browser secret file');
process.env.BROWSER_WS_PATH=key;
require('./server.cjs');



