//! TCP socket discovery straight from the kernel via NETLINK_SOCK_DIAG,
//! with socket inodes mapped back to PIDs through /proc/<pid>/fd.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::io;
use std::mem::size_of;
use std::net::{Ipv4Addr, Ipv6Addr};

use crate::procfs;
use crate::rules;

#[derive(Debug, Clone)]
pub struct Listener {
    pub port: u16,
    pub pid: i32,
    pub command: String,
    pub addresses: Vec<String>,
}

#[derive(Debug, Default)]
pub struct SocketScan {
    pub listening: Vec<Listener>,
    pub inbound: HashMap<u16, u32>,
    pub pids_by_port: HashMap<u16, HashSet<i32>>,
}

#[derive(Debug, Clone)]
struct RawSocket {
    state: u8,
    port: u16,
    address: String,
    inode: u32,
    uid: u32,
}

/// Holds the inode -> PID mapping between scans so the expensive walk over every
/// process's fds only happens when a new listening socket shows up.
#[derive(Default)]
pub struct SocketScanner {
    owners: HashMap<u32, Vec<i32>>,
    scans_since_full_walk: u32,
    docker: (f64, HashMap<u16, Vec<i32>>),
}

/// Forked workers can start sharing an existing socket; catch them eventually.
const FULL_WALK_EVERY: u32 = 10;

const SOCK_DIAG_BY_FAMILY: u16 = 20;
const NLMSG_ERROR: u16 = 2;
const NLMSG_DONE: u16 = 3;
const TCP_ESTABLISHED: u8 = 1;
const TCP_SYN_RECV: u8 = 3;
const TCP_LISTEN: u8 = 10;

#[repr(C)]
#[derive(Clone, Copy)]
struct NlMsgHdr {
    len: u32,
    kind: u16,
    flags: u16,
    seq: u32,
    pid: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct InetDiagSockId {
    sport: [u8; 2],
    dport: [u8; 2],
    src: [u8; 16],
    dst: [u8; 16],
    iface: u32,
    cookie: [u32; 2],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct InetDiagReqV2 {
    family: u8,
    protocol: u8,
    ext: u8,
    pad: u8,
    states: u32,
    id: InetDiagSockId,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct InetDiagMsg {
    family: u8,
    state: u8,
    timer: u8,
    retrans: u8,
    id: InetDiagSockId,
    expires: u32,
    rqueue: u32,
    wqueue: u32,
    uid: u32,
    inode: u32,
}

#[repr(C)]
struct Request {
    hdr: NlMsgHdr,
    req: InetDiagReqV2,
}

struct Fd(i32);
impl Drop for Fd {
    fn drop(&mut self) {
        unsafe { libc::close(self.0) };
    }
}

fn dump(family: u8, states: u32) -> io::Result<Vec<RawSocket>> {
    let fd = unsafe {
        libc::socket(
            libc::AF_NETLINK,
            libc::SOCK_DGRAM | libc::SOCK_CLOEXEC,
            libc::NETLINK_SOCK_DIAG,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let fd = Fd(fd);

    let request = Request {
        hdr: NlMsgHdr {
            len: size_of::<Request>() as u32,
            kind: SOCK_DIAG_BY_FAMILY,
            flags: (libc::NLM_F_REQUEST | libc::NLM_F_DUMP) as u16,
            seq: 1,
            pid: 0,
        },
        req: InetDiagReqV2 {
            family,
            protocol: libc::IPPROTO_TCP as u8,
            ext: 0,
            pad: 0,
            states,
            id: unsafe { std::mem::zeroed() },
        },
    };
    let mut addr: libc::sockaddr_nl = unsafe { std::mem::zeroed() };
    addr.nl_family = libc::AF_NETLINK as u16;
    let sent = unsafe {
        libc::sendto(
            fd.0,
            &request as *const Request as *const libc::c_void,
            size_of::<Request>(),
            0,
            &addr as *const libc::sockaddr_nl as *const libc::sockaddr,
            size_of::<libc::sockaddr_nl>() as u32,
        )
    };
    if sent < 0 {
        return Err(io::Error::last_os_error());
    }

    let mut out = Vec::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = unsafe { libc::recv(fd.0, buf.as_mut_ptr() as *mut libc::c_void, buf.len(), 0) };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        let data = &buf[..n as usize];
        let mut off = 0;
        while off + size_of::<NlMsgHdr>() <= data.len() {
            let hdr: NlMsgHdr = unsafe { std::ptr::read_unaligned(data[off..].as_ptr() as *const _) };
            let len = hdr.len as usize;
            if len < size_of::<NlMsgHdr>() || off + len > data.len() {
                return Ok(out);
            }
            match hdr.kind {
                NLMSG_DONE => return Ok(out),
                NLMSG_ERROR => return Err(io::Error::other("netlink error")),
                SOCK_DIAG_BY_FAMILY if len >= size_of::<NlMsgHdr>() + size_of::<InetDiagMsg>() => {
                    let msg: InetDiagMsg = unsafe {
                        std::ptr::read_unaligned(data[off + size_of::<NlMsgHdr>()..].as_ptr() as *const _)
                    };
                    out.push(RawSocket {
                        state: msg.state,
                        port: u16::from_be_bytes(msg.id.sport),
                        address: format_addr(msg.family, &msg.id.src),
                        inode: msg.inode,
                        uid: msg.uid,
                    });
                }
                _ => {}
            }
            off += (len + 3) & !3;
        }
    }
}

fn format_addr(family: u8, raw: &[u8; 16]) -> String {
    if family == libc::AF_INET as u8 {
        Ipv4Addr::new(raw[0], raw[1], raw[2], raw[3]).to_string()
    } else {
        Ipv6Addr::from(*raw).to_string()
    }
}

/// Map socket inodes to every PID (among `pids`) holding them. Only our own
/// processes are readable, which is also all that `ss -p` can show without root.
fn inode_owners(wanted: &HashSet<u32>, pids: impl Iterator<Item = i32>) -> HashMap<u32, Vec<i32>> {
    let mut owners: HashMap<u32, Vec<i32>> = HashMap::new();
    if wanted.is_empty() {
        return owners;
    }
    for pid in pids {
        let Ok(dir) = fs::read_dir(format!("/proc/{pid}/fd")) else {
            continue;
        };
        for entry in dir.flatten() {
            let Ok(target) = fs::read_link(entry.path()) else {
                continue;
            };
            let Some(inode) = target
                .to_str()
                .and_then(|t| t.strip_prefix("socket:["))
                .and_then(|t| t.strip_suffix(']'))
                .and_then(|t| t.parse::<u32>().ok())
            else {
                continue;
            };
            if wanted.contains(&inode) {
                let list = owners.entry(inode).or_default();
                if !list.contains(&pid) {
                    list.push(pid);
                }
            }
        }
    }
    owners
}

/// Root-owned docker-proxy hides its fds, but its cmdline names the host port.
fn docker_proxies() -> HashMap<u16, Vec<i32>> {
    let mut map: HashMap<u16, Vec<i32>> = HashMap::new();
    for pid in procfs::pids() {
        let Ok(raw) = fs::read(format!("/proc/{pid}/cmdline")) else {
            continue;
        };
        if !raw.windows(12).any(|w| w == b"docker-proxy") {
            continue;
        }
        let args: Vec<&[u8]> = raw.split(|b| *b == 0).collect();
        if !args.first().is_some_and(|a| a.ends_with(b"docker-proxy")) {
            continue;
        }
        if let Some(port) = args
            .windows(2)
            .find(|w| w[0] == b"-host-port")
            .and_then(|w| std::str::from_utf8(w[1]).ok()?.parse().ok())
        {
            map.entry(port).or_default().push(pid);
        }
    }
    map
}

impl SocketScanner {
    fn resolve_owners(&mut self, wanted: &HashSet<u32>) {
        // An inode is unique while its socket lives, so known inodes keep their owners;
        // owners that exit are dropped in build() when their /proc entry is gone.
        let known = wanted.iter().all(|i| self.owners.contains_key(i));
        if known && self.scans_since_full_walk < FULL_WALK_EVERY {
            self.owners.retain(|i, _| wanted.contains(i));
            self.scans_since_full_walk += 1;
            return;
        }
        self.owners = inode_owners(wanted, procfs::pids());
        self.scans_since_full_walk = 0;
    }

    fn docker_proxies(&mut self) -> &HashMap<u16, Vec<i32>> {
        let now = crate::now();
        if now - self.docker.0 > 10.0 {
            self.docker = (now, docker_proxies());
        }
        &self.docker.1
    }

    /// Listening TCP sockets with ports in `ports`, their owning PIDs, and
    /// inbound connection counts.
    pub fn scan(&mut self, ports: std::ops::RangeInclusive<u16>) -> SocketScan {
        let states = (1 << TCP_LISTEN) | (1 << TCP_ESTABLISHED) | (1 << TCP_SYN_RECV);
        let mut raw = Vec::new();
        for family in [libc::AF_INET as u8, libc::AF_INET6 as u8] {
            if let Ok(mut v) = dump(family, states) {
                raw.append(&mut v);
            }
        }
        let uid = unsafe { libc::getuid() };
        let listening: Vec<&RawSocket> = raw
            .iter()
            .filter(|s| s.state == TCP_LISTEN && ports.contains(&s.port))
            .filter(|s| s.uid == uid || s.uid == 0)
            .collect();
        let wanted: HashSet<u32> = listening.iter().filter(|s| s.uid == uid && s.inode != 0).map(|s| s.inode).collect();
        self.resolve_owners(&wanted);
        let docker = if listening.iter().any(|s| s.uid == 0) {
            self.docker_proxies().clone()
        } else {
            HashMap::new()
        };
        build(&raw, &listening, &self.owners, &docker)
    }
}

fn build(
    raw: &[RawSocket],
    listening: &[&RawSocket],
    owners: &HashMap<u32, Vec<i32>>,
    docker: &HashMap<u16, Vec<i32>>,
) -> SocketScan {
    let own_pid = std::process::id() as i32;
    let mut by_port: BTreeMap<u16, BTreeMap<i32, Listener>> = BTreeMap::new();
    let mut pids_by_port: HashMap<u16, HashSet<i32>> = HashMap::new();

    for sock in listening {
        let pids = owners
            .get(&sock.inode)
            .or_else(|| docker.get(&sock.port).filter(|_| sock.uid == 0))
            .cloned()
            .unwrap_or_default();
        let named: Vec<(i32, String)> = pids
            .into_iter()
            .filter(|p| *p != own_pid)
            .filter_map(|p| Some((p, procfs::process(p)?.comm)))
            .collect();
        // IDE helpers leak listening fds into every child they spawn.
        if named.iter().any(|(_, c)| rules::IDE_SOCKET_OWNERS.contains(&c.as_str())) {
            continue;
        }
        for (pid, comm) in named {
            pids_by_port.entry(sock.port).or_default().insert(pid);
            let l = by_port
                .entry(sock.port)
                .or_default()
                .entry(pid)
                .or_insert_with(|| Listener {
                    port: sock.port,
                    pid,
                    command: comm,
                    addresses: Vec::new(),
                });
            if !l.addresses.contains(&sock.address) {
                l.addresses.push(sock.address.clone());
            }
        }
    }

    let mut inbound: HashMap<u16, u32> = by_port.keys().map(|p| (*p, 0)).collect();
    for sock in raw.iter().filter(|s| s.state != TCP_LISTEN) {
        if let Some(c) = inbound.get_mut(&sock.port) {
            *c += 1;
        }
    }

    SocketScan {
        listening: by_port.into_values().flat_map(|m| m.into_values()).collect(),
        inbound,
        pids_by_port,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn struct_sizes_match_kernel() {
        assert_eq!(size_of::<InetDiagSockId>(), 48);
        assert_eq!(size_of::<InetDiagReqV2>(), 56);
        assert_eq!(size_of::<InetDiagMsg>(), 72);
    }

    #[test]
    fn finds_own_listener_and_connection() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let _client = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
        let (_server, _) = listener.accept().unwrap();

        let raw = dump(libc::AF_INET as u8, 1 << TCP_LISTEN).unwrap();
        let mine = raw.iter().find(|s| s.port == port).expect("listener in dump");
        let owners = inode_owners(&HashSet::from([mine.inode]), procfs::pids());
        assert_eq!(owners[&mine.inode], vec![std::process::id() as i32]);
        assert_eq!(mine.uid, unsafe { libc::getuid() });

        let est = dump(libc::AF_INET as u8, 1 << TCP_ESTABLISHED).unwrap();
        assert!(est.iter().any(|s| s.port == port));
    }
}
