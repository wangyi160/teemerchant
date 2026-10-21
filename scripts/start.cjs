'use strict';
const {spawn}=require('node:child_process');
const child=spawn(process.env.PYTHON||'python',['service.py',...process.argv.slice(2)],{stdio:'inherit',windowsHide:true});
child.on('exit',code=>{process.exitCode=code??1;});
