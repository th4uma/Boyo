"""One review group per connected composition; deletion evidence stays direct."""
import hashlib

GROUPING_VERSION = 'related-review-direct-suggestions-v4'


def group_members(group):
    members=group.get('members')
    if members is None:
        members=[group.get('keep'),*group.get('keeps',[]),*[m['id'] for m in group['candidates']]]
    return list(dict.fromkeys(aid for aid in members if aid is not None))


def group_references(group):
    return list(dict.fromkeys(aid for aid in group.get('keeps',[group.get('keep')]) if aid is not None))


def member_context(group,aid):
    return group.get('contexts',{}).get(str(aid),group['id'])


def decision_contexts(group,ids):
    if 'contexts' not in group: return group['id']
    return {aid:member_context(group,aid) for aid in ids}


def merge_review_groups(assets,neighbors,groups,scores,album,review_neighbors=None):
    """Merge only presentation. Never add a new deletion candidate or infer its reference."""
    by_id={a['id']:a for a in assets}
    adjacency={aid:set() for aid in by_id}
    for aid,edges in (review_neighbors if review_neighbors is not None else neighbors).items():
        if aid not in by_id: continue
        for other in edges:
            if other in by_id and other!=aid:
                adjacency[aid].add(other); adjacency[other].add(aid)
    components,component_by_id=[],{}
    for aid in by_id:
        if aid in component_by_id: continue
        number=len(components); queue=[aid]; component_by_id[aid]=number; members=[]
        while queue:
            current=queue.pop(); members.append(current)
            for other in sorted(adjacency[current]):
                if other not in component_by_id:
                    component_by_id[other]=number; queue.append(other)
        components.append(members)
    clustered={}
    for group in groups:
        clustered.setdefault(component_by_id[group['keep']],[]).append(group)
    def rank(aid):
        a=by_id[aid]
        return not a['protected'],a.get('decision')!='keep',-scores.get(aid,0),a['path']
    merged=[]
    for number,stars in clustered.items():
        proposed={m['id'] for star in stars for m in star['candidates']}
        refs=sorted((aid for aid in components[number] if aid not in proposed and by_id[aid].get('decision')!='delete'),key=rank)
        # Every star has a retained reference, even if all its candidates were explicitly deleted.
        primary=refs[0]
        candidates=[]; contexts={}; ordered=[]
        stars=sorted(stars,key=lambda star:rank(star['keep']))
        for star in stars:
            contexts[str(star['keep'])]=star['id']
            ordered.append(star['keep'])
            for member in star['candidates']:
                candidates.append({**member,'reference':star['keep']})
                contexts[str(member['id'])]=star['id']
                ordered.append(member['id'])
        included=set(ordered)
        for aid in sorted(components[number],key=rank):
            if aid not in included:
                ordered.append(aid); included.add(aid); contexts[str(aid)]='manual'
        ordered=[primary,*[aid for aid in ordered if aid!=primary]]
        same_members=len(stars)==1 and set(ordered)==set(group_members(stars[0]))
        identity=stars[0]['id'] if same_members else hashlib.sha256(
            (GROUPING_VERSION+'|'+album+'|'+','.join(map(str,sorted(ordered)))).encode()).hexdigest()[:20]
        merged.append({**stars[0],'id':identity,'keep':primary,'keeps':refs,'members':ordered,
                       'candidates':candidates,'contexts':contexts,'merged_groups':len(stars),
                       'matches':{str(aid):sorted(other for other in neighbors.get(aid,{}) if other in included) for aid in ordered}})
    return merged
