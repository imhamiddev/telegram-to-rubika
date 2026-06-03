from rubpy import Client

with Client(name='my_session') as client:
    print("✅ لاگین موفق! session ذخیره شد.")
    me = client.get_me()
    print(me)