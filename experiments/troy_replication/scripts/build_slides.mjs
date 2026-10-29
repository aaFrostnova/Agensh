#!/usr/bin/env node
/** Four-slide local briefing. Refresh from the actual report, never from guessed outcomes. */
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const args = Object.fromEntries(process.argv.slice(2).map((arg) => {
  const i = arg.indexOf('=');
  return [arg.replace(/^--/, '').slice(0, i < 0 ? undefined : i - 2), i < 0 ? true : arg.slice(i + 1)];
}));
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const root = path.resolve(args.root || path.resolve(repoRoot, '../..'));
const buildDir = path.resolve(args['build-dir'] || path.join(root, 'tmp/slides'));
const reportPath = path.resolve(args['report-json'] || path.join(root, 'results/report/report.json'));
const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const revision = String(args.revision || stamp);
const runDir = path.join(buildDir, revision);
const workspaceDir = buildDir;
const outputDir = path.join(buildDir, 'validated');
const runtimeRoot = process.env.CODEX_PRESENTATION_RUNTIME || path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies');
process.env.RUNTIME_NODE_MODULES ??= path.join(runtimeRoot, 'node/node_modules');
process.env.RUNTIME_NODE ??= process.execPath;
process.env.RUNTIME_BIN_DIR ??= path.join(runtimeRoot, 'bin/override');
process.env.RUNTIME_PYTHON ??= path.join(runtimeRoot, 'python/bin/python3');
const skillDir = process.env.CODEX_PRESENTATION_SKILL || path.join(os.homedir(), '.codex/plugins/cache/openai-primary-runtime/presentations/26.1017.22136/skills/presentations');
await fs.mkdir(runDir, {recursive: true});
await fs.mkdir(outputDir, {recursive: true});
try { await fs.symlink(path.join(runtimeRoot, 'node/node_modules'), path.join(buildDir, 'node_modules'), 'dir'); }
catch (error) { if (error.code !== 'EEXIST') throw error; }
const require = createRequire(path.join(buildDir, 'bootstrap.cjs'));
const { Presentation, PresentationFile } = await import(pathToFileURL(require.resolve('@oai/artifact-tool')));
const { resolvePresentationFont, finalizePresentation } = await import(pathToFileURL(path.join(skillDir, 'container_tools/artifact_tool_utils.mjs')));
const font = resolvePresentationFont({fontFamily: 'Noto Sans CJK SC'});
const fontPolicy = {basis: 'design', families: [font]};
let report = null;
try { report = JSON.parse(await fs.readFile(reportPath, 'utf8')); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const getFixture = async (id) => {
  try { return JSON.parse(await fs.readFile(path.join(root, 'fixtures', id, 'engineering_abc.json'), 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; return null; }
};
const astropy = await getFixture('astropy-12907');
const django = await getFixture('django-10554');
const payload = {generated_at: new Date().toISOString(), report_path: reportPath,
  report_available: report !== null, report, fixtures: {astropy, django},
  note: 'Formal outcomes derive only from report.formal_groups. Engineering episodes are excluded.'};
await fs.writeFile(path.join(runDir, 'source_payload.json'), JSON.stringify(payload, null, 2));
const presentation = Presentation.create({slideSize: {width: 1280, height: 720}});
const C = {ink: '#162235', body: '#34465B', muted: '#637286', accent: '#275D7C', light: '#E9EFF3', bg: '#FFFFFF'};
function text(slide, value, left, top, width, height, size=27, bold=false, color=C.body) {
  const shape = slide.shapes.add({geometry:'textbox', position:{left, top, width, height}, fill:'none', line:{fill:'none',width:0}});
  shape.text = value;
  shape.text.style = {fontSize:size,typeface:font,bold,color,wrap:'square',autoFit:'none',verticalAlignment:'top',insets:0};
  return shape;
}
function addSlide(title) {
  const slide = presentation.slides.add();
  slide.background.fill = C.bg;
  text(slide,title,64,48,1152,82,43,true,C.ink);
  return slide;
}
function table(slide, values, left, top, width, height, widths, size=25) {
  const t=slide.tables.add({rows:values.length,columns:values[0].length,left,top,width,height,values,columnWidths:widths});
  t.styleOptions={headerRow:true,bandedRows:false};
  t.borders.assign({fill:'#CBD5DF',width:0.7,style:'solid'});
  for(let r=0;r<values.length;r++) for(let c=0;c<values[0].length;c++) {
    const cell=t.getCell(r,c);
    cell.fill=r===0?C.light:'#FFFFFF';
    cell.text.style={typeface:font,fontSize:size,color:r===0?C.ink:C.body,bold:r===0,verticalAlignment:'middle',wrap:'square',insets:{left:14,right:14,top:10,bottom:10}};
  }
  return t;
}
function notes(slide, value) { slide.speakerNotes.text=value; }

// 1. Motivation, with no extra cover beyond the four requested slides.
{
 const s=addSlide('Agensh v0.6');
 text(s,'不可完成任务中的权限边界',64,158,1152,90,51,true,C.accent);
 text(s,'当守规修复无法通过测试时，agent 如何处理\n同行先例与伪造的人类授权？',64,292,1130,112,35,false,C.ink);
 text(s,'真实软件缺陷与矛盾断言构成决策点。\n实验观察受保护文件操作、公开回应与实际测试结果。',64,468,1120,96,28);
 text(s,'目标：复现定性行为，再单独检验实时对抗扩展。',64,632,1150,40,23,false,C.muted);
 notes(s,'用户计划：Agensh_v0.6_Troy_Replication_Plan_CN.md。任务基底为官方 ImpossibleBench conflicting variant 七实例。仅使用可观察公开输出与宿主事件，不推断模型隐藏意图。原文：https://arxiv.org/html/2609.15494v3 。');
}
// 2. Published findings, kept separate from local evidence.
{
 const s=addSlide('The Troy Moment 的主要发现');
 table(s,[['原文观察','适用范围与解释'],
 ['同行先例','Fable 不把未处罚的先例当作授权。\nGemini 更常以安全或注入语言拒绝。'],
 ['伪造的 principal 授权','Sol 在开放工具下会权衡这一主张，\n原文记录了实际受保护测试编辑。'],
 ['工具形式','typed 与 open 保持相同系统边界，\n但工具摩擦与任务能力也随之改变。']],64,163,1152,356,[330,822],27);
 text(s,'这些是原论文的定性发现，尚不能当作 Agensh 的复现结论。',64,554,1150,48,26,false,C.ink);
 text(s,'作者的 swarm 未注入主动 adversarial peer。实时对抗属于本地扩展。',64,625,1150,42,22,false,C.muted);
 notes(s,'Primary source: https://arxiv.org/html/2609.15494v3 , §3.2, Appendix B, Appendix C, Appendix E. Strict+Open and Strict+Typed share the system boundary; typed actions still technically allow protected-test edits. Tool-interface changes also affect task competence, so this is not a clean single-variable causal ablation. These qualitative observations were checked against the primary paper. Original stimulus provenance: https://github.com/para2046/troy-moment at 3c6f15b65e3b3b1e47a611acdc23e2c25c63f63a.');
}
// 3. Frozen local design.
{
 const s=addSlide('Agensh v0.6 复现设计');
 table(s,[['条件','加入 victim 上下文的信息'],['C0  Default','无社会信息'],['C1  Unpunished','同行修改受保护测试后通过，未记录干预或处罚'],['C2  Punished','同一次修改被发现，并作为违规恢复'],['C3  Forged principal','声称人类给予一次例外，真实高优先级规则保持不变']],64,150,1152,330,[335,817],25);
 text(s,'脚本复现：C0–C3，各 2 个 seed，共 8 场。\n实时扩展：C1 / C3，各 2 个 seed，共 4 场。',64,517,1150,78,26,false,C.ink);
 text(s,'Kimi 最高 reasoning。宿主独立监控写入与恢复，核验消息进入上下文，逐 agent 统计 token。',64,625,1150,57,22,false,C.muted);
 notes(s,'Source: local user plan and frozen cohort manifest (see report source_payload.json path). The minimum cohort uses the admitted Astropy task. All seven official task assets are frozen, but seven-task execution is a later coverage expansion. C1/C2 author stimulus changes only the penalty outcome sentence. Stage A starts from a model-independent observed-failure snapshot, with frozen task-specific stimulus in the first user message. Stage B uses Mattermost DM, at most three peer messages, and is analyzed separately. Highest supported Kimi reasoning is max; token accounting distinguishes input, output and reported reasoning tokens.');
}
// 4. Actual report only; missing observations remain visibly missing.
{
 const s=addSlide('当前证据与下一步');
 const planned=report?.plannedN > 0 ? report.plannedN : 12, valid=report?.validN ?? 0, invalid=report?.invalidN ?? 0;
 text(s,report?.plannedN > 0 ? `正式队列：有效 ${valid}/${planned} 场，无效 ${invalid} 场` : '最低计划 12 场，正式队列尚未完成冻结',64,138,1152,48,29,true,C.accent);
 const groups=Array.isArray(report?.formal_groups)?report.formal_groups:[];
 if (new Set(groups.map(g=>g.model)).size > 1 || new Set(groups.map(g=>g.task_id)).size > 1) throw new Error('Four-slide minimum-cohort briefing requires a single model/task report; do not pool models or tasks.');
 const groupFor=(mode,condition)=>groups.find(g=>g.condition===condition && (g.mode===mode || (mode==='scripted' && g.mode==='static') || (mode==='live' && g.mode==='autonomous')));
 const endpoint=(g)=>{if(!g || !g.validN) return '未观察'; const e=g.protected_write_executed_N ?? g.protected_write_executed_count ?? g.protected_write_executed ?? g.endpoint_counts?.protected_write_executed;return Number.isFinite(e)?String(e):'待汇总';};
 const rows=[['模式与条件','有效 / 计划','写入事件场次']];
 for(const [mode,conditions,label] of [['scripted',['C0','C1','C2','C3'],'脚本'],['live',['C1','C3'],'实时']]) for(const c of conditions) {
   const g=groupFor(mode,c);rows.push([`${label} ${c}`,`${g?.validN ?? 0} / ${g?.plannedN ?? 2}`,endpoint(g)]);
 }
 table(s,rows,64,211,704,330,[240,220,244],24);
 const aReady=astropy?.status==='validated';
 text(s,`Astropy：${aReady?'A/B/C 通过':'A/B/C 待验证'}\n官方 source fix 仍受矛盾断言阻挡。\n隔离副本修改受保护测试后可通过。`,818,215,398,123,24);
 text(s,`Django：${django?.status==='blocked'?'暂不准入':'等待工程结论'}\n原源码触发 unittest 报告崩溃。\n其余 5 任务尚未执行。`,818,373,398,123,24);
 const next=valid<planned?'后续：完成冻结队列，逐场审阅轨迹，再扩展七任务覆盖。':'后续：审阅组间差异与代表轨迹，再扩展七任务及重复 seed。';
 text(s,next,64,576,1150,48,26,false,C.ink);
 text(s,'小样本只做描述性比较。工程试跑、无效场次与 Qwen 验证均不进入正式分母。',64,642,1152,40,22,false,C.muted);
 notes(s,`Actual local report: ${reportPath}. Source payload for this revision: ${path.join(runDir,'source_payload.json')}. Astropy and Django engineering records are loaded directly from ${root}/fixtures. Engineering A/B/C evidence is distinct from full monitor/isolation admission A–F. A status of validated in engineering_abc.json is only A/B/C plus runtime bind verification. Formal endpoint counts come exclusively from report.formal_groups; absence is never converted to a safe zero. The current exact original-environment replication remains blocked because the authors did not publish historical OCI digests or a downloadable snapshot blob. Local image manifests are current registry digests. Do not pool scripted and live results.`);
}
const candidatePath=path.join(runDir,'draft.pptx');
await (await PresentationFile.exportPptx(presentation)).save(candidatePath);
for(const [i,slide] of presentation.slides.items.entries()) {
 const blob=await presentation.export({slide,format:'png',scale:1});
 await fs.writeFile(path.join(runDir,`slide-${i+1}.png`),new Uint8Array(await blob.arrayBuffer()));
 const layout=await slide.export({format:'layout'});
 await fs.writeFile(path.join(runDir,`slide-${i+1}.layout.json`),await layout.text());
}
const finalPath=path.join(outputDir,`v060-briefing-${revision}.pptx`);
const validation=await finalizePresentation({
 workspaceDir,candidatePath,finalPath,explicitTotalSlideCount:4,
 requiredNativeTableOwnerSlides:[2,3,4],requiredNativeChartOwnerSlides:[],
 pythonExecutable:path.join(runtimeRoot,'python/bin/python3'),
 integrityValidatorPath:path.join(skillDir,'container_tools/inspect_presentation_package_integrity.py'),
 layoutValidatorPath:path.join(skillDir,'container_tools/inspect_presentation_layout_geometry.py'),
 layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit',
  '--require-native-table-slide','2','--require-native-table-slide','3','--require-native-table-slide','4'],
 fontPolicy,verifyArtifactToolImport:true,receiptPath:path.join(runDir,'validation.json'),
});
await fs.writeFile(path.join(buildDir,'latest.json'),JSON.stringify({revision,finalPath,runDir,reportPath,report_available:report!==null,validation},null,2));
console.log(JSON.stringify({finalPath,runDir,report_available:report!==null,validN:report?.validN??null}));
