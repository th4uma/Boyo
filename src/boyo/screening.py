"""A review profile, never a reset of decisions or learning feedback."""
SECONDARY = 'secondary-v1'
SECONDARY_STRENGTH = '高强度'


def is_secondary(settings):
    return settings.get('screening_mode') == SECONDARY


def suggestion_is_current(group, asset, stamp):
    """Only the decisions this pass actually examined may be reconsidered."""
    baseline = group.get('review_stamps', {}).get(str(asset['id']))
    return bool(baseline and baseline == stamp and asset['decision'] in {'pending', 'keep'})


def evidence_is_current(group, asset, root):
    from .fs import checked
    baseline = group.get('review_stamps', {}).get(str(asset['id']))
    if not baseline: return False
    try: stat = checked(root,asset['path'],True).stat()
    except (OSError,ValueError): return False
    return [stat.st_size,stat.st_mtime_ns] == baseline[:2]
