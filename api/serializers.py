def serialize_school(school):
    return {
        "id": school.id,
        "name": school.name,
    }


def serialize_debater(debater):
    return {
        "id": debater.id,
        "name": debater.name,
        "first_name": debater.first_name,
        "last_name": debater.last_name,
        "status": debater.get_status_display(),
        "school_id": debater.school_id if debater.school else None,
        "school_name": debater.school.name if debater.school else None,
    }
