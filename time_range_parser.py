import datetime


def parse_time_range(time_range):
    time_range = time_range.split("-")
    assert len(time_range) == 2
    for i, x in enumerate(time_range):
        if x == '' or x == '0':
            time_range[i] = 0 if i == 0 else 2 ** 31
            continue
        if '.' in x:
            xd, xt = x.split(".")
            xd = parse_date(xd)
            xt = datetime.datetime.strptime(xt, "%H%M%S").time()
        else:
            xd, xt = x, datetime.time(0)
            xd = parse_date(xd) + datetime.timedelta(days=1)
        time_range[i] = int(datetime.datetime.combine(xd.date(), xt).timestamp())
    return time_range


def parse_date(date_text):
    if len(date_text) == 6:
        return datetime.datetime.strptime(date_text, '%y%m%d')
    elif len(date_text) == 8:
        return datetime.datetime.strptime(date_text, '%Y%m%d')
    raise ValueError()
