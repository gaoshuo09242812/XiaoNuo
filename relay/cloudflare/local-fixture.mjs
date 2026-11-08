// Only a local acceptance fixture. No account or public deployment is used.
import { Miniflare } from '../../data/tools/cloudflare-runtime/node_modules/miniflare/dist/src/index.js';
import { readFile,writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const [configPath,readyPath]=process.argv.slice(2),config=JSON.parse(await readFile(configPath,'utf8'));
const mf=new Miniflare({modules:true,scriptPath:fileURLToPath(new URL('./worker.mjs',import.meta.url)),
  compatibilityDate:'2026-07-01',durableObjects:{RELAY:{className:'DeviceRelay',useSQLite:true}},cf:false,
  host:'127.0.0.1',port:0,https:true,httpsKeyPath:config.key,httpsCertPath:config.cert,
  bindings:{HOST_TOKEN_SHA256:config.host_hash,DEVICE_HASHES:JSON.stringify(config.device_hashes)}});
try{
  const url=await mf.ready;await writeFile(readyPath,JSON.stringify({port:Number(url.port)}));
  await new Promise(resolve=>process.stdin.once('data',resolve));
}finally{await mf.dispose();}
