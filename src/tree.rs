use std::collections::{HashMap, HashSet};

use crate::procfs::Proc;
use crate::sockets::SocketScan;

pub struct ProcessTree<'a> {
    pub processes: &'a HashMap<i32, Proc>,
    children: HashMap<i32, Vec<i32>>,
    listeners_by_port: HashMap<u16, HashSet<i32>>,
    ports_by_pid: HashMap<i32, HashSet<u16>>,
}

impl<'a> ProcessTree<'a> {
    pub fn new(processes: &'a HashMap<i32, Proc>, sockets: &SocketScan) -> Self {
        let mut children: HashMap<i32, Vec<i32>> = HashMap::new();
        for p in processes.values() {
            children.entry(p.ppid).or_default().push(p.pid);
        }
        for c in children.values_mut() {
            c.sort_unstable();
        }
        let mut listeners_by_port = sockets.pids_by_port.clone();
        for l in &sockets.listening {
            listeners_by_port.entry(l.port).or_default().insert(l.pid);
        }
        let mut ports_by_pid: HashMap<i32, HashSet<u16>> = HashMap::new();
        for (port, pids) in &listeners_by_port {
            for pid in pids {
                ports_by_pid.entry(*pid).or_default().insert(*port);
            }
        }
        Self { processes, children, listeners_by_port, ports_by_pid }
    }

    /// Reloaders and workers sharing a socket are one service; follow shared
    /// sockets transitively.
    pub fn service(&self, pid: i32) -> HashSet<i32> {
        let mut result = HashSet::from([pid]);
        let mut pending = vec![pid];
        while let Some(cur) = pending.pop() {
            for port in self.ports_by_pid.get(&cur).into_iter().flatten() {
                for peer in self.listeners_by_port.get(port).into_iter().flatten() {
                    if result.insert(*peer) {
                        pending.push(*peer);
                    }
                }
            }
        }
        result
    }

    pub fn contains_other_service(&self, pid: i32, service: &HashSet<i32>) -> bool {
        let mut pending = vec![pid];
        let mut seen = HashSet::new();
        while let Some(cur) = pending.pop() {
            if !seen.insert(cur) {
                continue;
            }
            if self.ports_by_pid.contains_key(&cur) && !service.contains(&cur) {
                return true;
            }
            pending.extend(self.children.get(&cur).into_iter().flatten());
        }
        false
    }

    /// Depth-first walk of `root`'s subtree, skipping branches owned by other services.
    pub fn descendants(&self, root: i32, service: &HashSet<i32>) -> Vec<(&'a Proc, usize)> {
        let mut out = Vec::new();
        let mut seen = HashSet::new();
        let mut stack = vec![(root, 0usize)];
        while let Some((pid, depth)) = stack.pop() {
            if !seen.insert(pid) {
                continue;
            }
            let Some(proc) = self.processes.get(&pid) else { continue };
            out.push((proc, depth));
            for child in self.children.get(&pid).into_iter().flatten().rev() {
                if !self.contains_other_service(*child, service) {
                    stack.push((*child, depth + 1));
                }
            }
        }
        out
    }
}
