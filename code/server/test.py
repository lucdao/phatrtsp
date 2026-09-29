#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib, GstRtsp
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
ROLE_ADMIN = "admin-role"


import os
import shutil

def create_virtual_playlist(file_list, virtual_dir="/server/virtual_playlist"):
    """
    Creates a folder of sequential symlinks pointing to arbitrary files.
    """
    # 1. Create or clear the virtual directory
    if os.path.exists(virtual_dir):
        shutil.rmtree(virtual_dir)
    os.makedirs(virtual_dir)

    valid_files = 0
    # 2. Create sequential symlinks
    for index, file_path in enumerate(file_list):
        if os.path.exists(file_path):
            # Format: 0000.mp4, 0001.mp4, etc.
            symlink_name = f"{index:04d}.mp4" 
            symlink_path = os.path.join(virtual_dir, symlink_name)
            
            # Create the shortcut
            os.symlink(file_path, symlink_path)
            valid_files += 1
        else:
            logging.warning(f"Skipping missing file: {file_path}")
            
    logging.info(f"Created virtual playlist with {valid_files} files in {virtual_dir}")
    return virtual_dir
class SplitMuxFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, folder_path, pattern="*.mp4"):
        super().__init__()
        self.folder_path = folder_path
        self.pattern = pattern

    def do_create_element(self, _url):
        # The location must be an absolute path + pattern
        # e.g., /home/user/videos/*.mp4
        full_pattern = os.path.join(self.folder_path, self.pattern)
        
        
        # pipeline_str = (
        #     f"splitmuxsrc location=\"{full_pattern}\" ! "
        #     "h264parse update-timecode=true ! "
        #     "video/x-h264,stream-format=byte-stream ! " # Added this
        #     "rtph264pay name=pay0 pt=96 config-interval=1" # Ensures headers are sent often
        # )

        pipeline_str = (
            f"splitmuxsrc location=\"{full_pattern}\" name=src "
            "src. ! queue ! h264parse update-timecode=true ! video/x-h264,stream-format=byte-stream ! rtph264pay name=pay0 pt=96 config-interval=1 "
            "src. ! queue ! aacparse ! rtpmp4apay name=pay1 pt=97"
        )
        
        logging.info(f"Launching pipeline: {pipeline_str}")
        return Gst.parse_launch(f"( {pipeline_str} )")

class FileBroadcaster:
    def __init__(self, rtspServer):
        self.rtspServer = rtspServer
        self.rtspServer.set_service("8552")
        self.factories = [] # Keep references
        # --- 1. SETUP AUTHENTICATION ---
        self.auth: GstRtspServer.RTSPAuth = GstRtspServer.RTSPAuth()
        self.auth.set_supported_methods(GstRtsp.RTSPAuthMethod.DIGEST)
        self.admin_token: GstRtspServer.RTSPToken = self._initialize_role(ROLE_ADMIN)
        #self.rtspServer.set_auth(self.auth)
        self.rtspServer.attach(None)

    def _initialize_role(self, role_name: str) -> GstRtspServer.RTSPToken:
        """
        Initialize and return an RTSPToken for a given role.
        
        :param role_name: The role name (e.g. "admin" or "user").
        :return: A GstRtspServer.RTSPToken representing the role.
        """
        token: GstRtspServer.RTSPToken = GstRtspServer.RTSPToken()
        token.set_string("media.factory.role", role_name)
        return token
        
    def broadcast_folder(self, mount_path: str, folder_path: str):
        # 1. Check if files exist
        if not os.path.exists(folder_path):
            logging.error(f"Folder not found: {folder_path}")
            return

        # 2. Create the factory
        factory = SplitMuxFactory(folder_path, pattern="*.mp4")
        factory.add_role_from_structure(self._get_role_structure(ROLE_ADMIN))
        factory.set_suspend_mode(GstRtspServer.RTSPSuspendMode.NONE)
        factory.set_stop_on_disconnect(False)
        # Add digest authentication credentials.
        #self.auth.add_digest("admin", "Khonghoinhieu1", self.admin_token)
        # Shared = True means everyone watches the same "TV Channel"
        # If Client A seeks, Client B will also jump (standard for shared RTSP).
        # Set to False if you want every client to have their own playback session.
        factory.set_shared(False)
        factory.set_stop_on_disconnect(True)
        # 3. Handle End-Of-Stream (Looping)
        # When the playlist ends, we want to restart it.
        factory.connect("media-constructed", self.on_media_constructed)
        
        # 4. Attach to server
        mnts = self.rtspServer.get_mount_points()
        mnts.add_factory(mount_path, factory)
        self.factories.append(factory)
        
        logging.info(f"Stream ready at rtsp://admin:Khonghoinhieu1@127.0.0.1:8552{mount_path}")
    def _get_role_structure(self, role_name: str) -> Gst.Structure:
        """
        Create and return a Gst.Structure for the given role.
        
        :param role_name: Role name.
        :return: A Gst.Structure defining access and construction permissions.
        """
        structure: Gst.Structure = Gst.Structure.new_empty(role_name)
        # Allow access to the media factory.
        structure.set_value("media.factory.access", True)
        # Only the admin role is allowed to construct (create) streams.
        structure.set_value("media.factory.construct", role_name == ROLE_ADMIN)
        return structure
    def on_media_constructed(self, _factory, media):
        """Sets up the loop mechanism."""
        elem = media.get_element()
        bus = elem.get_bus()
        bus.add_signal_watch()
        
        def _on_msg(_bus, msg):
            if msg.type == Gst.MessageType.EOS:
                logging.info("Playlist finished, looping back to start...")
                # Seek to 0 (Start)
                elem.seek_simple(Gst.Format.TIME,
                                 Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                 0)
        bus.connect("message", _on_msg)

if __name__ == "__main__":
    Gst.init(None)
    loop = GLib.MainLoop()
    
    server = GstRtspServer.RTSPServer()
    broadcaster = FileBroadcaster(server)
    # my_custom_playlist = [
    #     "/server/playlist/5015D_2026_02_25_16_49_24.mp4",
    #     "/server/playlist/5015D_2026_02_25_16_51_03.mp4",
    #     "/mnt/minio/test/5015D/5015D_2026_02_25_16_49_24.mp4",
    #     "/mnt/minio/test/5015D/5015D_2026_02_25_16_51_03.mp4"
    # ]

    #assets_dir = create_virtual_playlist(my_custom_playlist)

    # EDIT THIS PATH
    #assets_dir = "/server/playlist"
    assets_dir = "/mnt/minio/video-normal/5015D/2026/02/27"
    # Broadcast all *.mp4 files in 'assets_dir' as one stream
    broadcaster.broadcast_folder("/playlist", assets_dir)
    
    logging.info("Server running...")
    loop.run()
