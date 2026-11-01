def validate_v2(obj,manifest):
    frames={f['image_id']:f for f in manifest['frames']};obs={};errors=[];resolved=[]
    for o in obj['observations']:
        if o['id'] in obs:errors.append({'kind':'duplicate_observation','id':o['id']})
        obs[o['id']]=o
        if o['image_id'] not in frames:errors.append({'kind':'unknown_image','id':o['id']})
    seen=set();previous=None
    for e in obj['events']:
        if e['id'] in seen:errors.append({'kind':'duplicate_event','id':e['id']})
        seen.add(e['id']);refs=e['observation_ids'];anchor=e['anchor_observation_id']
        if not refs or any(x not in obs for x in refs) or anchor not in refs:
            errors.append({'kind':'invalid_observation_reference','event':e['id']});continue
        o=obs[anchor]
        if o['image_id'] not in frames:continue
        f=frames[o['image_id']]
        ref_times=[frames[obs[x]['image_id']]['timestamp_seconds'] for x in refs if obs[x]['image_id'] in frames]
        # Canonical evidence endpoint, not the time an unseen action was performed.
        # A single fixed rule avoids trusting a generated time or silently sorting.
        f=max((frames[obs[x]['image_id']] for x in refs if obs[x]['image_id'] in frames),key=lambda x:x['timestamp_seconds'])
        t=f['timestamp_seconds']
        # No generated time is accepted, and no sort is used to repair semantics.
        if previous is not None and t<previous:errors.append({'kind':'chronology_inversion','event':e['id'],'previous_seconds':previous,'host_seconds':t})
        previous=t
        resolved.append({**e,'image_id':f['image_id'],'host_start_seconds':min(ref_times),'host_seconds':t,'host_time_meaning':'end of cited evidence range, not inferred action execution time','host_image_path':f['path'],'host_image_sha256':f['sha256']})
    for q in obj['questions']:
        if any(x not in obs for x in q['observation_ids']):errors.append({'kind':'invalid_question_reference'})
    unclear=[e['id'] for e in obj['events'] if e['status']=='unclear']
    held=len(obj['events']) if errors else len(unclear)
    return {'status':'blocked' if errors else 'pending_operator_review','structural_errors':errors,'resolved_events':resolved,'held_event_ids':[e['id'] for e in obj['events']] if errors else unclear,'held_events':held,'total_events':len(obj['events']),'hold_rate':held/len(obj['events']) if obj['events'] else 1,'semantic_truth_verified':False,'operator_approved':False}
