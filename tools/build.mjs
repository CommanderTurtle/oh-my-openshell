// SPDX-FileCopyrightText: 2026 CommanderTurtle
// SPDX-License-Identifier: AGPL-3.0-only
import { mkdir, copyFile } from 'node:fs/promises';
const target = 'src/omosh/static/vendor';
await mkdir(target, { recursive: true });
const result = await Bun.build({entrypoints:['web/vendor.js'], outdir:target, naming:'vendor.js', minify:true, target:'browser'});
if (!result.success) throw new AggregateError(result.logs, 'Browser dependency build failed');
await copyFile('node_modules/@xterm/xterm/css/xterm.css', `${target}/xterm.css`);
await copyFile('node_modules/@xterm/xterm/LICENSE', `${target}/XTERM-LICENSE`);
await copyFile('node_modules/@xterm/addon-fit/LICENSE', `${target}/FIT-LICENSE`);
await copyFile('node_modules/marked/LICENSE', `${target}/MARKED-LICENSE`);
await copyFile('node_modules/dompurify/LICENSE', `${target}/DOMPURIFY-LICENSE`);
console.log('Local browser dependencies built.');
