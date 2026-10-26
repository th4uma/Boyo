"""Read-only learning evidence for the UI; no image decoding or training."""
import json
from datetime import datetime


def learning_report(store,runtime):
    from .preferences import load_model
    with store.connect() as db:
        counts=[dict(row) for row in db.execute('SELECT source,decision,COUNT(*) AS n FROM feedback GROUP BY source,decision')]
        evidence=db.execute("""SELECT COUNT(*) AS photos,COUNT(DISTINCT a.album) AS albums
            FROM feedback f JOIN assets a ON a.id=f.asset_id
            WHERE f.source='manual' AND a.role='ordinary_jpeg' AND a.decision=f.decision
            AND (a.protected=0 OR f.decision='keep') AND a.size=f.size AND a.mtime_ns=f.mtime_ns""").fetchone()
        row=db.execute("SELECT data FROM learning_state WHERE key='automatic'").fetchone()
        state=json.loads(row['data']) if row else {}
        new=db.execute("SELECT COUNT(*) AS n FROM feedback_events WHERE source='manual' AND id>?",(state.get('revision',0),)).fetchone()['n']
    manual=sum(r['n'] for r in counts if r['source']=='manual')
    keep=sum(r['n'] for r in counts if r['source']=='manual' and r['decision']=='keep')
    adopted=sum(r['n'] for r in counts if r['source']=='adopt')
    model=load_model(runtime,store.path)
    enabled=[];scores=[]
    if model:
        for key,title in (('rank','同构图排序'),('retention','保留构图差异'),('quality','技术问题复核顺序')):
            head=model.get(key)
            if not head:continue
            enabled.append(title)
            heads=head.values() if key=='quality' else [head]
            for item in heads:
                accuracy,baseline=item.get('accuracy'),item.get('baseline')
                if isinstance(accuracy,(float,int)) and isinstance(baseline,(float,int)):
                    scores.append(f'{title}：验证一致率 {accuracy:.1%}，基础算法 {baseline:.1%}（只代表本次独立拍摄组验证）')
    if enabled:reason='已通过独立拍摄组验证；用于之后的分析，不改变已有留删。'
    elif not manual:reason='还没有主动留删记录，照常筛图即可自动积累。'
    elif evidence['albums']<2:reason=f"可用主动反馈来自 {evidence['albums']} 个拍摄组，至少需要 2 个组进行交叉验证。继续正常筛下一组即可。"
    elif not state:reason='尚无完成的验证结果；积累新选择并空闲后会自动尝试。'
    elif state.get('enabled'):reason='上次模型当前不可用（文件缺失或版本不兼容）；继续使用基础算法，后续自动重新验证。'
    elif state.get('diagnostics'):
        reason='；'.join(dict.fromkeys(item['reason'] for item in state['diagnostics'].values() if not item.get('enabled') and item.get('reason')))
    elif state.get('pairs',0)<30:reason='上次同构图留删比较不足 30 对；其他学习项也尚未通过验证，继续正常筛图。'
    else:reason='上次验证未启用个人项；可能缺少有效样本或未优于基础算法。旧结果没有记录更细的原因，下次验证会补充。'
    updated=state.get('updated');when=datetime.fromtimestamp(updated).strftime('%Y-%m-%d %H:%M') if updated else '尚未完成验证'
    details=[f'当前启用：{"、".join(enabled) if enabled else "基础算法（个人学习项未启用）"}',
        reason,f'最近完成验证：{when}',f'上次验证使用 {state.get("photos",0)} 张主动选择，形成 {state.get("pairs",0)} 对同构图留删比较。',
        f'当前记录：主动保留 {keep} 张、主动待删 {manual-keep} 张；批量采纳 {adopted} 张。',
        f'其中符合当前文件和决定状态的主动反馈 {evidence["photos"]} 张，来自 {evidence["albums"]} 个拍摄组。',
        f'上次验证后新增 / 修改 / 撤销 {new} 次主动选择；上次比较数不代表当前已重新计算。']
    details+=scores or ['暂无可靠的提升数字；不会显示虚构的“了解程度”。']
    details+=['学习仅影响同构图排序、构图差异保留和按相机区分的技术问题复核顺序；图集不参与。',
        '正常保留、待删、撤销即可。积累约 12 次新选择后，空闲约 12 秒自动尝试验证，两次启动至少间隔 2 分钟；文件整理和分析优先。',
        '所有处理都在本机。无需设置学习率或训练轮数；“同时分析照片数”和“额外间隔”仅控制运行资源。']
    return dict(manual=manual,keep=keep,delete=manual-keep,adopted=adopted,albums=evidence['albums'],eligible=evidence['photos'],pairs=state.get('pairs',0),
        updated=when,new=new,enabled=bool(enabled),enabled_items=enabled,reason=reason,details='\n\n'.join(details),state=state)
