import time
import secrets
import grpc
import argparse
import zmq

from queue import Queue
from collections import deque
from concurrent import futures
from threading import Thread, Semaphore
from multiprocessing import Event
from typing import Type, Dict, List
from spacetime import Node

from rlcompetition.match_server import server_app
from rlcompetition.data_model import ServerState, Player, _Observation, Observation
from rlcompetition.config import get_environment, available_environments
from rlcompetition.BaseEnvironment import BaseEnvironment
from rlcompetition.util import is_port_in_use
from rlcompetition.rl_logging import init_logging, get_logger

from .grpc_gen.server_pb2 import QuickMatchReply, QuickMatchRequest
from .grpc_gen.server_pb2_grpc import MatchmakerServicer, add_MatchmakerServicer_to_server
from .RankingDatabase import RankingDatabase


logger = get_logger()


def match_server_args_factory(tick_rate: int, realtime: bool, observations_only: bool, env_config_string: str):
    """ Helper factory to make a argument dictionary for servers with varying ports """

    def match_server_args(port):
        arg_dict = {
            "tick_rate": tick_rate,
            "port": port,
            "realtime": realtime,
            "observations_only": observations_only,
            "config": env_config_string
        }
        return arg_dict

    return match_server_args


class MatchMakingHandler(MatchmakerServicer):
    """ GRPC connection handler.

        Clients will connect to the server and call this function to request a match. """

    def GetMatch(self, request, context):
        # Prepare ZeroMQ connection
        # We use ZeroMQ to communicate between the handler and the matchmaking server
        context = zmq.Context()
        socket = context.socket(zmq.REQ)
        socket.connect("ipc://matchmaker_requests")

        # Request a new match
        # print(request.SerializeToString(), QuickMatchRequest.FromString(request.SerializeToString()))
        socket.send(request.SerializeToString())
        return QuickMatchReply.FromString(socket.recv())


class MatchProcessJanitor(Thread):
    """ Simple thread to manage the lifetime of a game server. Will start the game server and
        close it when the game is finished and release any resources it was holding. """

    def __init__(self,
                 match_limit: Semaphore,
                 ports_to_use_queue: Queue,
                 database: RankingDatabase,
                 env_class: Type[BaseEnvironment],
                 match_server_args: Dict,
                 player_list: List,
                 whitelist: List = None):
        super().__init__()
        self.match_limit = match_limit
        self.match_server_args = match_server_args
        self.env_class = env_class
        self.ports_to_use_queue = ports_to_use_queue
        self.database = database
        self.player_list = player_list
        self.whitelist = whitelist
        self.ready = Event()

    def run(self) -> None:
        port = self.match_server_args['port']
        observation_type: Type[_Observation] = Observation(self.env_class.observation_names())

        # App blocks until the server has ended
        app = Node(server_app, server_port=port, Types=[Player, ServerState])
        app.start(self.env_class, observation_type, self.match_server_args, self.whitelist, self.ready)
        del app

        # Cleanup
        self.ports_to_use_queue.put(port)
        for user in self.player_list:
            self.database.logoff(user)

        self.match_limit.release()


class MatchmakingThread(Thread):

    def __init__(self,
                 starting_port,
                 hostname,
                 max_simultaneous_games,
                 env_class,
                 tick_rate,
                 realtime,
                 observations_only,
                 env_config_string):
        super().__init__()

        self.players_per_game = env_class(env_config_string).min_players
        self.env_class = env_class
        self.hostname = hostname

        # Prepare our context and sockets
        context = zmq.Context()
        self.socket = context.socket(zmq.ROUTER)
        self.socket.bind("ipc://matchmaker_requests")
        print("Matchmaker thread listening...")

        # Semaphore for tracking the total number of games running
        self.match_limit = Semaphore(max_simultaneous_games)

        # Helper function to make arguments for match threads
        self.create_match_server_args = match_server_args_factory(tick_rate=tick_rate,
                                                                  realtime=realtime,
                                                                  observations_only=observations_only,
                                                                  env_config_string=env_config_string)

        # Keep track of the ports we can use and iterate through them as we start new servers
        self.ports_to_use = Queue()
        max_port = starting_port + 2 * max_simultaneous_games
        for port in range(starting_port, max_port):
            if not is_port_in_use(port):
                self.ports_to_use.put(port)
            else:
                logger.warn("Skipping port {}, already in use.".format(port))

        if self.ports_to_use.qsize() < max_simultaneous_games:
            raise OSError("Port range {} through {} does not have enough unallocated ports "
                          "to hold {} simultaneous games".format(starting_port, max_port, max_simultaneous_games))

        self.database = RankingDatabase("test.sqlite")

    def run(self) -> None:
        match_requests = deque()

        while True:
            # Grab a new request
            identity, _, serialized_request = self.socket.recv_multipart()
            request = QuickMatchRequest.FromString(serialized_request)

            # Login user and handle any errors
            username, password = request.username, request.password
            login_result = self.database.login(username, password)

            if login_result == RankingDatabase.LoginResult.NoUser:
                self.database.set(username, password)
                self.database.login(username, password)

            elif login_result == RankingDatabase.LoginResult.LoginDuplicate:
                response = QuickMatchReply(username=username, server="FAIL", auth_key="FAIL", ranking=0.0,
                                           response="Failed to login: Cannot login twice at the same time.")
                self.socket.send_multipart((identity, b"", response.SerializeToString()))
                continue

            elif login_result == RankingDatabase.LoginResult.LoginFail:
                response = QuickMatchReply(username=username, server="FAIL", auth_key="FAIL", ranking=0.0,
                                           response="Failed to login: Wrong password.")
                self.socket.send_multipart((identity, b"", response.SerializeToString()))
                continue

            # Add request to the queue and generate a token for them
            match_requests.append((identity, request, secrets.token_hex(32)))

            # Once we have enough players for a game, start a game server and send the coordinates
            if len(match_requests) >= self.players_per_game:
                self.match_limit.acquire()
                new_players = [match_requests.pop() for _ in range(self.players_per_game)]
                whitelist = [player[2] for player in new_players]
                usernames = [player[1].username for player in new_players]

                match_port = self.ports_to_use.get()
                match_server_args = self.create_match_server_args(port=match_port)
                match_janitor = MatchProcessJanitor(match_limit=self.match_limit,
                                                    ports_to_use_queue=self.ports_to_use,
                                                    database=self.database,
                                                    env_class=self.env_class,
                                                    match_server_args=match_server_args,
                                                    player_list=usernames,
                                                    whitelist=whitelist)
                match_janitor.start()

                database_entries = self.database.get_multi(*usernames)
                database_entries = {name: ranking for name, _, ranking in database_entries}
                match_janitor.ready.wait()

                for identity, request, auth_key in new_players:
                    response = QuickMatchReply(username=request.username,
                                               server='{}:{}'.format(self.hostname, match_port),
                                               auth_key=auth_key,
                                               ranking=database_entries[request.username],
                                               response="")

                    self.socket.send_multipart((identity, b"", response.SerializeToString()))


def serve(args):
    # Start the separate matchmaking thread
    matchmaker_thread = MatchmakingThread(
        hostname=args['hostname'],
        starting_port=args['game_port'],
        max_simultaneous_games=args['max_games'],
        env_class=get_environment(args['environment']),
        tick_rate=args['tick_rate'],
        realtime=args['realtime'],
        observations_only=args['observations_only'],
        env_config_string=args['config']
    )
    matchmaker_thread.start()

    # Start the GRPC callback server
    server = grpc.server(futures.ThreadPoolExecutor())
    add_MatchmakerServicer_to_server(MatchMakingHandler(), server)
    server.add_insecure_port('[::]:{}'.format(args['matchmaking_port']))
    server.start()
    try:
        one_day = 3600 * 24
        while True:
            time.sleep(one_day)
    except KeyboardInterrupt:
        server.stop(0)


def start_matchmaking_server(environment: str = 'test',
                             hostname: str = 'localhost',
                             matchmaking_port: int = 50051,
                             game_port: int = 21450,
                             max_games: int = 1,
                             tick_rate: int = 60,
                             realtime: bool = False,
                             observations_only: bool = False,
                             config: str = ''):
    serve(locals())


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--environment", "-e", type=str, default="test",
                        help="The name of the environment. Choices are: {}".format(available_environments()))
    parser.add_argument("--hostname", type=str, default='localhost',
                        help="Hostname to start the matchmaking and game servers on. Defaults to 'localhost'")
    parser.add_argument("--matchmaking-port", type=int, default=50051,
                        help="Port to start matchmaking server on.")
    parser.add_argument("--game-port", type=int, default=21450,
                        help="Port to start game servers on. Will use a range starting at this port to this port"
                             "plus the number of games.")
    parser.add_argument("--max-games", "-m", type=int, default=1,
                        help="Number of games to run in parallel on this server.")
    parser.add_argument("--tick-rate", "-t", type=int, default=60,
                        help="The max tick rate that the server will run on.")
    parser.add_argument("--realtime", "-r", action="store_true",
                        help="With this flag on, the server will not wait for all of the clients to respond.")
    parser.add_argument("--observations-only", '-f', action='store_true',
                        help="With this flag on, the server will not push the true state of the game to the clients "
                             "along with observations")
    parser.add_argument("--config", '-c', type=str, default="",
                        help="Config string that will be passed into the environment constructor.")

    command_line_args = parser.parse_args()

    serve(vars(command_line_args))


if __name__ == '__main__':
    logger = init_logging()
    main()
