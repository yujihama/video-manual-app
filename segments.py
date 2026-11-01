"""Bounded model requests and deterministic joins on the original video timeline."""
import copy

FRAMES_PER_REQUEST = 40
CONTEXT_FRAMES = 2

def plan_segments(frames):
    plans = []
    for start in range(0, len(frames), FRAMES_PER_REQUEST):
        stop = min(start + FRAMES_PER_REQUEST, len(frames))
        plans.append({'id': f'B{len(plans)+1:05d}',
                      'image_ids': [f['image_id'] for f in frames[max(0, start-CONTEXT_FRAMES):stop]],
                      'owned_image_ids': [f['image_id'] for f in frames[start:stop]]})
    return plans

def segment_prompt(plan):
    context = [x for x in plan['image_ids'] if x not in set(plan['owned_image_ids'])]
    return ('\nこれは長い動画の連続区間です。前後の未収録操作を補完しない。'
            f'今回の担当画像IDは {plan["owned_image_ids"]}。'
            f'先頭の文脈用画像IDは {context}。'
            '文脈用画像は直前の状態との比較にだけ使い、それだけで完結する操作をeventsへ再掲しない。'
            '各eventの最後の根拠画像は今回の担当画像から選ぶ。'
            '区間境界で操作が未完了なら見えた状態だけを記述し、完了を推測しない。')

def merge_segments(parts, frames):
    """Namespace IDs; reject context-only events rather than silently deleting them."""
    merged = {'title': parts[0][1]['title'], 'observations': [], 'events': [], 'questions': []}
    times = {f['image_id']: f['timestamp_seconds'] for f in frames}
    for plan, original in parts:
        obj = copy.deepcopy(original); prefix = plan['id'] + '_'
        observations = {o['id']: o for o in obj['observations']}
        for e in obj['events']:
            cited = [observations[x]['image_id'] for x in e['observation_ids'] if x in observations]
            if cited and all(x in times for x in cited):
                latest = max(cited, key=times.get)
                if latest not in plan['owned_image_ids']:
                    raise ValueError('区間境界で直前区間の操作が重複しました。生成結果を保持し、手順書の出力を保留しました。')
            e['id'] = prefix + e['id']
            e['observation_ids'] = [prefix+x for x in e['observation_ids']]
            e['anchor_observation_id'] = prefix+e['anchor_observation_id']
        for o in obj['observations']: o['id'] = prefix+o['id']
        for q in obj['questions']: q['observation_ids'] = [prefix+x for x in q['observation_ids']]
        for key in ('observations', 'events', 'questions'): merged[key].extend(obj[key])
    return merged
