from polymarket_weather_bot.run_bot import run_forever
import os
import time

if __name__ == '__main__':
    store, engine = run_forever()
    print('Running on http://127.0.0.1:%s' % os.getenv('BOT_PORT', '8080'))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
