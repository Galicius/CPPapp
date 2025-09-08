# notifier.py
def notify_user(user, slot):
    # TODO: replace with Postmark/Twilio
    print(f"[notify][{user['channel']}] -> {user['address']} : {slot['date_str']} {slot['time_str']} | {slot['location']} | {slot['categories']}")
