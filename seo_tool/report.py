"""
Sorties du diagnostic :
  - seo_issues.json    : le snapshot complet (consommé par le volet 2 + ré-exécutions)
  - seo_dashboard.html : dashboard autonome (onglet 1), données inlinées, ouvrable sans serveur
"""

from __future__ import annotations
import json
import os

from seo_tool import config


def write_json(snapshot: dict, path: str | None = None) -> str:
    path = path or os.path.join(config.OUTPUT_DIR, "seo_issues.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return path


def render_dashboard_html(snapshot: dict) -> str:
    """Retourne le HTML du dashboard avec les données inlinées (réutilisé par la webapp)."""
    return _TEMPLATE.replace("/*DATA*/", json.dumps(snapshot, ensure_ascii=False))


def write_dashboard(snapshot: dict, path: str | None = None) -> str:
    path = path or os.path.join(config.OUTPUT_DIR, "seo_dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_dashboard_html(snapshot))
    return path


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Diagnostic SEO — Le Petit Lunetier</title>
<style>
  :root{--bg:#f6f7f9;--card:#fff;--line:#e6e9ee;--ink:#1d2126;--muted:#6b7280;
        --accent:#2E8FA6;--good:#3f9d57;--amber:#b3771e;--bad:#c2415e}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{display:flex;align-items:center;gap:16px;padding:16px 22px;background:var(--card);border-bottom:1px solid var(--line)}
  header h1{font-size:1.05rem;margin:0;flex:1}
  .who{font-size:.76rem;color:var(--muted)}
  .wrap{padding:22px;max-width:1180px;margin:0 auto}
  .top{display:flex;gap:20px;flex-wrap:wrap;align-items:center;margin-bottom:20px}
  .gauge{width:120px;height:120px;border-radius:50%;display:flex;align-items:center;justify-content:center;
    flex-direction:column;color:#fff;flex:0 0 auto}
  .gauge .v{font-size:1.7rem;font-weight:800;line-height:1} .gauge .l{font-size:.62rem;opacity:.9;text-transform:uppercase;letter-spacing:.06em}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;flex:1;min-width:280px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:14px 16px}
  .card .lbl{font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
  .card .sc{font-size:1.5rem;font-weight:700;margin-top:3px}
  .card .fix{font-size:.78rem;color:var(--muted);margin-top:2px}
  .bar{height:6px;border-radius:4px;background:#eef0f2;margin-top:8px;overflow:hidden}
  .bar > span{display:block;height:100%}
  h2{font-size:.82rem;text-transform:uppercase;letter-spacing:.05em;color:#9aa1ab;margin:24px 0 10px}
  table{width:100%;border-collapse:collapse;font-size:.82rem;background:var(--card);border:1px solid var(--line);border-radius:11px;overflow:hidden}
  th,td{text-align:left;padding:9px 12px;border-bottom:1px solid #eef0f2;vertical-align:top}
  th{font-size:.68rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:#fafbfc}
  tr:last-child td{border-bottom:none}
  .tag{font-size:.66rem;font-weight:700;padding:2px 8px;border-radius:6px;white-space:nowrap}
  .t-collection_seo{background:#e8f3f6;color:#1f6b78}.t-image_alt{background:#fdeede;color:#9a6411}
  .t-product_meta{background:#eef0fb;color:#4150a8}.t-translation_missing{background:#e9f6ec;color:#2f7d46}
  .sev-high{color:var(--bad);font-weight:700}.sev-medium{color:var(--amber);font-weight:600}.sev-low{color:var(--muted)}
  .mono{font-family:ui-monospace,Menlo,monospace;font-size:.76rem;color:#516072}
  .filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
  .fbtn{font-size:.74rem;padding:5px 11px;border:1px solid var(--line);background:#fff;border-radius:7px;cursor:pointer}
  .fbtn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
  .muted{color:var(--muted)} .small{font-size:.72rem}
</style></head>
<body>
<header><h1>Diagnostic SEO — Le Petit Lunetier</h1><span class="who" id="meta"></span></header>
<div class="wrap">
  <div class="top">
    <div class="gauge" id="gauge"></div>
    <div class="kpis" id="kpis"></div>
  </div>
  <h2>Collections prioritaires <span class="muted small">trafic organique × pertinence marché lunetterie</span></h2>
  <div id="collections"></div>
  <h2>Backlog complet <span class="muted small" id="count"></span></h2>
  <div class="filters" id="filters"></div>
  <div id="tablewrap"></div>
</div>
<script>
const SNAP = /*DATA*/;
const col = s => s==null?'#9ca3af':s>=85?'#3f9d57':s>=60?'#b3771e':'#c2415e';
const CATLBL = {collection_seo:'Pages collections',image_alt:'Alt-text images',product_meta:'Meta produits',translation_missing:'Traductions'};
document.getElementById('meta').textContent = `${SNAP.shop} · ${new Date(SNAP.generated_at).toLocaleString('fr-FR')}`;
const g=SNAP.scores.global, gd=document.getElementById('gauge');
gd.style.background=col(g); gd.innerHTML=`<div class="v">${g==null?'—':g}</div><div class="l">Score global</div>`;
document.getElementById('kpis').innerHTML = Object.entries(SNAP.scores.categories).map(([k,c])=>`
  <div class="card"><div class="lbl">${c.label}</div><div class="sc" style="color:${col(c.score)}">${c.score==null?'—':c.score}</div>
  <div class="fix">${c.score==null?'non évalué (scope manquant)':c.to_fix.toLocaleString('fr-FR')+' à corriger · '+c.affected+'/'+c.total+' objets'}</div>
  <div class="bar"><span style="width:${c.score==null?0:c.score}%;background:${col(c.score)}"></span></div></div>`).join('');

const issues = SNAP.issues;
document.getElementById('count').textContent = `(${issues.length.toLocaleString('fr-FR')} issues)`;
let filter='all';
const types=['all',...Object.keys(CATLBL)];
const fc=document.getElementById('filters');
fc.innerHTML=types.map(t=>`<button class="fbtn${t==='all'?' on':''}" data-t="${t}">${t==='all'?'Tout':CATLBL[t]}</button>`).join('');
fc.querySelectorAll('.fbtn').forEach(b=>b.onclick=()=>{filter=b.dataset.t;
  fc.querySelectorAll('.fbtn').forEach(x=>x.classList.toggle('on',x===b)); render();});

function ctx(i){
  const c=i.context||{};
  if(i.type==='collection_seo') return `${c.title||''} · ${c.products_count||0} produits`;
  if(i.type==='image_alt') return c.product_title||'';
  if(i.type==='product_meta') return c.title||'';
  if(i.type==='translation_missing') return `clé « ${c.key} » → ${c.locale}${c.outdated?' (à mettre à jour)':''}`;
  return '';
}
function renderCollections(){
  const cmap={};
  issues.filter(i=>i.type==='collection_seo').forEach(i=>{ if(!cmap[i.handle]) cmap[i.handle]={h:i.handle,c:i.context,p:i.priority_score}; });
  const rows=Object.values(cmap).sort((a,b)=>b.p-a.p);
  const badge=t=>t===3?'<span class="tag" style="background:#e9f6ec;color:#2f7d46">marché · trafic prouvé</span>'
    :t===2?'<span class="tag" style="background:#fff3e0;color:#b9770e">marché · opportunité</span>'
    :'<span class="tag" style="background:#eef0f2;color:#6b7280">autre</span>';
  document.getElementById('collections').innerHTML=`<table><thead><tr>
    <th>Collection</th><th>Catégorie marché</th><th>Trafic organique /mois</th><th>Volume mot-clé</th><th>Produits</th><th>Manque</th><th>Priorité</th></tr></thead><tbody>
    ${rows.map(r=>{const c=r.c;return `<tr>
      <td class="mono">${r.h}</td>
      <td>${c.market_label||'<span class="muted">—</span>'}${c.target_kw?`<div class="small muted">${c.target_kw}</div>`:''}</td>
      <td><b>${(c.organic_traffic||0).toLocaleString('fr-FR')}</b></td>
      <td>${c.search_volume?c.search_volume.toLocaleString('fr-FR'):'<span class="muted">—</span>'}</td>
      <td>${(c.products_count||0).toLocaleString('fr-FR')}</td>
      <td class="small mono">${(c.missing||[]).join(', ')}</td>
      <td>${badge(c.tier)}</td></tr>`;}).join('')}
  </tbody></table>`;
}
function render(){
  const rows=issues.filter(i=>filter==='all'||i.type===filter).slice(0,400);
  document.getElementById('tablewrap').innerHTML=`<table><thead><tr>
    <th>Type</th><th>Objet</th><th>Champ manquant</th><th>Contexte</th><th>Sévérité</th><th>Prio</th></tr></thead>
    <tbody>${rows.map(i=>`<tr>
      <td><span class="tag t-${i.type}">${CATLBL[i.type]}</span></td>
      <td class="mono">${i.handle||i.object_id}</td>
      <td class="mono">${i.field}</td>
      <td>${ctx(i)}</td>
      <td class="sev-${i.severity}">${i.severity}</td>
      <td>${i.priority_score}</td></tr>`).join('')}</tbody></table>
    ${issues.filter(i=>filter==='all'||i.type===filter).length>400?'<p class="muted small">(400 premières affichées)</p>':''}`;
}
renderCollections();
render();
</script></body></html>"""
