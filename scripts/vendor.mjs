import {mkdir, copyFile} from 'node:fs/promises';

await mkdir('tracker/static/vendor/swagger', {recursive: true});
for (const [source, destination] of [
  ['lightweight-charts/dist/lightweight-charts.standalone.production.js', 'lightweight-charts.js'],
  ['lightweight-charts/LICENSE', 'LICENSE'],
  ...['swagger-ui-bundle.js', 'swagger-ui.css', 'LICENSE', 'NOTICE', 'swagger-ui-bundle.js.LICENSE.txt']
    .map(name => ['swagger-ui-dist/' + name, 'swagger/' + name]),
]) {
  await copyFile('node_modules/' + source, 'tracker/static/vendor/' + destination);
}
