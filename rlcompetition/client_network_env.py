from multiprocessing import Pipe
import numpy as np
from typing import Tuple
import pickle
import logging

import spacetime
from spacetime import Application, Dataframe
from .data_model import Player, ServerState
from .frame_rate_keeper import FrameRateKeeper
from random import randint

CLIENT_TICK_RATE = 60

logger = logging.getLogger(__name__)


def game_is_terminal(dataframe):
    return dataframe.read_all(ServerState)[0].terminal


def client_app(dataframe, remote, parent_remote, player_name, player_class, dimension_names):
    # parent_remote.close() # we are in a separate thread, not process

    # Create player class and add ourselves to the dataframe
    dataframe.pull()
    dataframe.checkout()

    player = player_class(name=player_name)
    dataframe.add_one(player_class, player)

    dataframe.commit()
    dataframe.push()

    # Check to see if it worked
    dataframe.pull()
    dataframe.checkout()
    if dataframe.read_one(player_class, player.pid) is not None:
        logger.info("Connected to server, waiting for game to start...")
    else:
        logger.info("Server rejected adding your player, perhaps the max player limit has been reached.")
        exit()

    fr = FrameRateKeeper(CLIENT_TICK_RATE)

    # Wait for game to start
    while player.number == -1:
        fr.tick()
        dataframe.pull()
        dataframe.checkout()
        player = dataframe.read_one(player_class, player.pid)

    logger.info("Game has started, acting as player number {}".format(player.number))

    while not player.turn:
        fr.tick()
        dataframe.pull()
        dataframe.checkout()
        player = dataframe.read_one(player_class, player.pid)

    remote.send({dimension_name: getattr(player, dimension_name) for dimension_name in dimension_names})
    logger.debug("First turn for player {} started".format(player.number))

    try:
        while True:
            cmd, data = remote.recv()

            if cmd == 'step':

                if not game_is_terminal(dataframe):
                    action = data
                    player.action = action
                    player.ready_for_action_to_be_taken = True
                    dataframe.commit()
                    dataframe.push()

                    print("sent action")

                    while not player.turn or player.ready_for_action_to_be_taken:
                        fr.tick()
                        dataframe.pull()
                        dataframe.checkout()
                        player = dataframe.read_one(player_class, player.pid)

                dimensions = {dimension_name: getattr(player, dimension_name) for dimension_name in dimension_names}

                reward = player.reward_from_last_turn
                terminal = game_is_terminal(dataframe)

                if terminal:
                    winners = pickle.loads(dataframe.read_all(ServerState)[0].winners)
                    player.acknowledges_game_over = True
                    dataframe.commit()
                    dataframe.push()
                else:
                    winners = None

                remote.send((dimensions, reward, terminal, winners))

            elif cmd == 'close':
                dataframe.delete_one(player_class, player)
                remote.close()
                break

            elif cmd == 'get_server_state':
                server_state = dataframe.read_all(ServerState)[0]

                env_class_name = server_state.env_class_name
                env_dimensions = server_state.env_dimensions
                terminal = server_state.terminal
                winners = server_state.winners
                serialized_state = server_state.serialized_state

                remote.send((env_class_name, env_dimensions, terminal, winners, serialized_state))

            else:
                raise NotImplementedError
    except KeyboardInterrupt:
        print('client_app: got KeyboardInterrupt')


class ClientNetworkEnv:

    def __init__(self, server_hostname, port, player_name):
        self.remote, app_remote = Pipe()

        # Get the dimensions required for the
        df = Dataframe("dimension_getter_{}".format(player_name), [ServerState], details=(server_hostname, port))
        df.pull()
        df.checkout()
        dimension_names: [str] = df.read_all(ServerState)[0].env_dimensions
        del df

        player_class = Player(dimension_names)

        self.player_client = Application(client_app,
                                         dataframe=(server_hostname, port),
                                         Types=[player_class, ServerState],
                                         version_by=spacetime.utils.enums.VersionBy.FULLSTATE)

        self.player_client.start_async(remote=app_remote,
                                       parent_remote=self.remote,
                                       player_name=player_name,
                                       player_class=player_class,
                                       dimension_names=dimension_names)

        self.first_observation = self.remote.recv()

    def close(self):
        self.remote.send(("close", None))
        self.player_client.join()

    def get_first_observation(self):
        return self.first_observation

    def step(self, action: str) -> Tuple[dict, float, bool, int]:
        """
        Compute a single step in the game.

        Parameters
        ----------
        action : int

        Returns
        -------
        new_observation : np.ndarray
        reward : float
        terminal : bool
        winners: list - Only matters if terminal = True
        """
        self.remote.send(('step', action))
        dimensions, reward, terminal, winners = self.remote.recv()
        return dimensions, reward, terminal, winners

    def get_server_state(self) -> Tuple[str, Tuple[int], bool, str, str]:
        self.remote.send(("get_server_state", None))
        server_state = self.remote.recv()

        return server_state