'use strict';
const {spawn} = require('node:child_process');
const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
const child = spawn(require('electron'), ['.', ...process.argv.slice(2)], {stdio:'inherit', env, windowsHide:true});
child.on('exit', code => { process.exitCode = code ?? 1; });
