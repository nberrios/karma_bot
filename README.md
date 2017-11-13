# A simple karma tracking bot implemented with curio

## Disclaimer ##
karma_bot must be run using python3.5 or higher

## Install Requirements ##
1. Clone this repo
2. Run `pip install -r requirements.txt`

## Setup database ##
1. Run `python karma_bot.py initdb`

## Adding the bot to your IRC server ##
1. Run `python karma_bot.py <server:port>`. If the port is not specified, it will default to 6667

Once the bot is connected to your IRC server, send the message `.karmabot help` to see a list of available commands.

