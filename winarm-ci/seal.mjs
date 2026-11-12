import {createCipheriv,createDecipheriv,randomBytes} from 'node:crypto';
import {readFile,writeFile} from 'node:fs/promises';
const MAGIC=Buffer.from('DAE1'), AAD=Buffer.from('DevAiAye ARM sealed envelope v1');
const [op,input,output]=process.argv.slice(2);
const encoded=process.env.ARM_SEAL_KEY; delete process.env.ARM_SEAL_KEY;
if(!/^[0-9a-f]{64}$/.test(encoded??'')||!['seal','open'].includes(op)||!input||!output)throw Error('invalid arguments or key');
const key=Buffer.from(encoded,'hex'), bytes=await readFile(input);if(bytes.length>268435456)throw Error('input too large');
let result;
if(op==='seal'){const nonce=randomBytes(12),cipher=createCipheriv('aes-256-gcm',key,nonce);cipher.setAAD(AAD);const ciphertext=Buffer.concat([cipher.update(bytes),cipher.final()]);result=Buffer.concat([MAGIC,nonce,cipher.getAuthTag(),ciphertext]);}
else {if(bytes.length<32||!bytes.subarray(0,4).equals(MAGIC))throw Error('invalid envelope');const decipher=createDecipheriv('aes-256-gcm',key,bytes.subarray(4,16));decipher.setAAD(AAD);decipher.setAuthTag(bytes.subarray(16,32));result=Buffer.concat([decipher.update(bytes.subarray(32)),decipher.final()]);}
await writeFile(output,result,{flag:'wx',mode:0o600});key.fill(0);
