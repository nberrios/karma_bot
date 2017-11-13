#!/usr/bin/env python
import re
import sys
import time
import sqlite3
from curio import run, socket, sleep, run_in_thread
from curio.errors import TaskCancelled
from curio.socket import AF_INET, SOCK_STREAM, SOL_SOCKET, SO_REUSEADDR


encoding = 'UTF-8'

karma_pattern = r'(?:((@?[aA-zZ\w]+)|[\'"].+?[\'"]))(?:(\+{2,}|\-{2,}))'
karma_reg = re.compile(karma_pattern)

class ConnectionError(Exception):
    pass


class KarmaBot(object):

    def __init__(self, server, port, user='kbot', nick=None, db=None):
        self.server = server
        self.port = port
        self.user = user
        self.nick = nick if nick else 'KarmaBot'
        self.command_nick = self.nick.lower()
        self._socket = None
        self._connected = False
        self.exit_code = '.{0} quit'.format(self.nick.lower())
        if not db:
            db = sqlite3.connect('karma.db', check_same_thread=False)
        self.db = db
        self.cursor = db.cursor()

    def _create_socket(self):
        # ---- socket is called synchronously but uses async methods
        if not self._socket:
            sock = socket.socket(AF_INET, SOCK_STREAM)
            sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
            self._socket = sock
        return self._socket

    @property
    def socket(self):
        if not self._socket:
            self._create_socket()
        return self._socket

    async def _connect(self):
        try:
            await self.socket.connect((self.server, self.port))
        except socket.error as error:
            await self.close_connection()
            print('Connection failed: {0}'.format(error))
            raise ConnectionError
        else:
            self._connected = True
            await self.login()
            await sleep(3)

    @property
    def conn(self):
        return self.socket

    async def reconnect(self):
        if self._connected:
            await self.disconnect()
        await self.connect()

    async def disconnect(self):
        await self.leave_server()
        await self.close_connection()

    async def leave_server(self):
        if self._connected:
            await self.conn.send(bytes('QUIT \n', encoding))

    async def close_connection(self):
        if self._socket:
            await self._socket.close()
        self._socket = None

    async def login(self):
        await self.conn.send(bytes('CAPS LS\n', encoding))
        await self.conn.send(bytes('NICK {0}\n'.format(self.nick), encoding))
        await self.conn.send(bytes('USER {nickname} 8 * {nickname}\n'.format(nickname=self.nick), encoding))

    async def send_msg(self, message, target=None):
        if target is None:
            print('Received message but no target channel:', message)
        await self.conn.send(bytes('PRIVMSG {target} :{message}\n'.format(
            target=target,
            message=message), encoding))

    async def respond_to_ping(self):
        await self.conn.send(bytes('PONG :pingis\n', encoding))

    async def join_rooms(self, channels=None, summoner=None):
        if not channels:
            return
        if not summoner:
            summoner = 'A mysterious force'
        channels_string = ','.join(channels)
        await self.conn.send(bytes('JOIN {0}\n'.format(channels_string), encoding))
        recv_msg = ''
        while recv_msg.find('End of /NAMES list.') == -1:
            recv_msg = await self.conn.recv(2048)
            recv_msg = recv_msg.decode(encoding).strip('\n\r')
            print(recv_msg)
        for channel in channels:
            await self.send_msg('{0} has summoned me. '
                          'Type \'.{1} help\' for commands'.format(summoner, self.command_nick), target=channel)

    async def leave_rooms(self, channels=None, summoner=None):
        if not channels:
            return
        if not summoner:
            summoner = 'A mysterious force'
        channels_string = ','.join(channels)
        for channel in channels:
            await self.send_msg('Disconnecting... (requested by: {0})'.format(summoner), target=channel)
        await self.conn.send(bytes('PART {0}\n'.format(channels_string), encoding))

    async def list_karma(self, modifier='top'):
        statement = 'SELECT * FROM users '
        if modifier == 'top':
            heading = 'Top karma:\n'
            statement = statement + 'ORDER BY KARMA desc LIMIT 3'
        elif modifier == 'bottom':
            heading = 'Bottom karma:\n'
            statement = statement + 'ORDER BY KARMA asc LIMIT 3'
        else:
            heading = ''
            statement = statement + 'WHERE name = :name'

        res = await self.execute(statement, {'name': modifier})
        res = res.fetchall()
        if not res:
            res = [(0, modifier, 0)]
        return heading, res

    async def _process_karma(self, target, change, room):
        token = 'increased'
        sign = 1
        if change[:1] == '-':
            token = 'decreased'
            sign = -1
        user = await self.query_user(target)
        if not user:
            user = await self.create_user(target)
        amount = len(change) - 1
        current_amount = user[2]
        new_amount = current_amount + sign * amount
        await self.update_user_karma(user[0], new_amount)
        await self.send_msg('{target}\'s karma has been {token} to {new_amount}.'.format(
            target=target,
            token=token,
            new_amount=new_amount), target=room)

    async def query_user(self, username):
        res = await self.execute('SELECT * from users where name = :name', {'name': username})
        user = res.fetchone()
        return user

    async def create_user(self, username):
        try:
            await self.execute('INSERT INTO users (name, karma) VALUES (:name, 0)', {
            'name': username})
            await self.commit()
        except Exception as error:
            await self.rollback()
            print('Error creating user:', error)
        else:
            return await self.query_user(username)

    async def commit(self):
        return await run_in_thread(self.db.commit)

    async def rollback(self):
        return await run_in_thread(self.db.rollback)

    async def execute(self, *args, **kwargs):
        return await run_in_thread(self.cursor.execute, *args, **kwargs)

    async def update_user_karma(self, userid, new_karma):
        '''user is a tuple of (id, name, karma)'''
        try:
            await self.execute('UPDATE users SET karma = :karma where id = :id', {
                'karma': new_karma,
                'id': userid})
            await self.commit()
        except Exception as error:
            await self.rollback()
            print('Error updating user karma:', error)

    async def listen(self):
        while True:
            # ---- karma can only be granted in channels, not DM's with karmabot
            karma_eligible = False
            recv_msg = await self.conn.recv(2048)
            recv_msg = recv_msg.decode(encoding).strip('\n\r')
            print(recv_msg)
            if recv_msg.find('PRIVMSG') != -1:
                name = recv_msg.split('!', 1)[0][1:]
                source, message = recv_msg.split('PRIVMSG', 1)[1].split(':', 1)
                source = source.strip()
                channel = name
                if source[0] == '#':
                    # ---- we came from a channel
                    channel = source
                    karma_eligible = True
                if message[:14] == '.{0} join'.format(self.command_nick):
                    target = message.split(' ', 2)
                    if len(target) != 3:
                        await self.send_msg(
                            'Uses: .{0} [join|leave] [#channel1, #channel2, ...]'.format(self.command_nick),
                            target=channel)
                    else:
                        target = target[2]
                        rooms = list(target.split(' '))
                        await self.join_rooms(channels=rooms, summoner=name)
                if message[:15] == '.{0} leave'.format(self.command_nick):
                    target = message.split(' ', 2)
                    if len(target) != 3:
                        await self.send_msg('Uses: .{0} [join|leave] [#channel1, #channel2, ...]'.format(self.command_nick), target=channel)
                    else:
                        target = target[2]
                        rooms = list(target.split(' '))
                        await self.leave_rooms(channels=rooms, summoner=name)
                if karma_eligible:
                    karma_changed = karma_reg.findall(message)
                    if karma_changed:
                        for group in karma_changed:
                            await self._process_karma(group[0], group[2], channel)
                if message[:20] == '.{0} list-karma'.format(self.command_nick):
                    args = message.split(' ', 2)
                    if len(args) != 3:
                        await self.send_msg('Uses: .{0} list-karma [top|bottom|name]'.format(self.command_nick), target=channel)
                    else:
                        modifier = args[2]
                        header, results = await self.list_karma(modifier)
                        await self.send_msg(header, target=channel)
                        for result in results:
                            await self.send_msg('{0}: {1}'.format(result[1], result[2]), target=channel)
                if message[:14] == '.{0} help'.format(self.command_nick):
                    await self.send_msg('Commands available:', target=channel)
                    await self.send_msg('.{0} [join|leave] [#server1, #server2, ...]'.format(self.command_nick), target=channel)
                    await self.send_msg('.{0} list-karma [top|bottom|name]'.format(self.command_nick), target=channel)
                    await self.send_msg('.{0} quit'.format(self.command_nick), target=channel)
                if message.rstrip() == self.exit_code:
                    await self.conn.send(bytes('QUIT \n', encoding))
                    return
            else:
                if recv_msg.find('PING :') != -1:
                    await self.respond_to_ping()

async def main(server, port):
    bot = KarmaBot(server, port)
    try:
        await bot._connect()
    except ConnectionError as error:
        print('Connection Error:', error)
        bot.db.close()
        sys.exit(1)
    try:
        await bot.listen()
    except (TaskCancelled, KeyboardInterrupt) as signal:
        print('Shutting down... because:', signal)
        bot.db.close()
        sys.exit(0)
    except Exception as error:
        print('Error:', error)
        bot.db.close()
        sys.exit(1)
    else:
        bot.db.close()
        sys.exit(0)

def initdb():
    '''run this if you wanna init the database'''
    db = sqlite3.connect('karma.db')
    cursor = db.cursor()
    cursor.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, '
                   'name TEXT, '
                   'karma INTEGER)')
    try:
        db.commit()
    except Exception as error:
        print('Got error initializing the database', error)
        db.rollback()
        db.close()
    else:
        db.close()

if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise RuntimeError(
            'Insufficient arguments. '
            'Run `python karma_bot.py help` for options')
    argument = sys.argv[1]
    if argument == 'help':
        print('Usage (must be using python3.5+):')
        print('To initialize the database: `python karma_bot.py initdb`')
        print('To run the karma bot: `python karma_bot.py <server:port>`')
        sys.exit(0)
    elif argument == 'initdb':
        initdb()
        print('Database initialized')
        sys.exit(0)
    else:
        address = argument.split(':', 1)
        server = address[0]
        port = 6667
        if len(address) == 2:
            port = int(address[1])
    run(main(server, port), with_monitor=True)
