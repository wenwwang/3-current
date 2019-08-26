import shutil
import sys
import time

import h5py
import numpy as np
from scipy.linalg import expm

np.seterr(over="ignore")


# http://xoroshiro.di.unimi.it/splitmix64.c
def rand_seed(x):
    x = np.uint64(x)
    rng = np.zeros(17, dtype=np.uint64)
    for i in range(16):
        x += np.uint64(0x9E3779B97F4A7C15)
        z = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        rng[i] = z ^ (z >> np.uint64(31))
    return rng


# http://xoroshiro.di.unimi.it/xorshift1024star.c
def rand_uint(rng):
    s0 = rng[rng[16]]
    p = (int(rng[16]) + 1) & 15
    rng[16] = p
    s1 = rng[p]
    s1 ^= s1 << np.uint64(31)
    rng[p] = s1 ^ s0 ^ (s1 >> np.uint64(11)) ^ (s0 >> np.uint64(30))
    return rng[p] * np.uint64(1181783497276652981)


def rand_jump(rng):
    JMP = np.array((0x84242f96eca9c41d,
                    0xa3c65b8776f96855, 0x5b34a39f070b5837, 0x4489affce4f31a1e,
                    0x2ffeeb0a48316f40, 0xdc2d9891fe68c022, 0x3659132bb12fea70,
                    0xaac17d8efa43cab8, 0xc4cb815590989b13, 0x5ee975283d71c93b,
                    0x691548c86c1bd540, 0x7910c41d10a1e6a5, 0x0b5fc64563b3e2a8,
                    0x047f7684e9fc949d, 0xb99181f2d8f685ca, 0x284600e3f30e38c3
                    ), dtype=np.uint64)

    t = np.zeros(16, dtype=np.uint64)
    for i in range(16):
        for b in range(64):
            if JMP[i] & (np.uint64(1) << np.uint64(b)):
                for j in range(16):
                    t[j] ^= rng[(np.uint64(j) + rng[16]) & np.uint64(15)]
            rand_uint(rng)

    for j in range(16):
        rng[(np.uint64(j) + rng[16]) & np.uint64(15)] = t[j]


def create_1(filename=None, overwrite=False, seed=None,
             Nx=16, Ny=4, mu=0.0, tp=0.0, U=6.0, dt=0.115, L=40,
             nflux=0,
             n_delay=16, n_matmul=8, n_sweep_warm=200, n_sweep_meas=2000,
             period_eqlt=8, period_uneqlt=0,
             meas_bond_corr=1, meas_energy_corr=0, meas_nematic_corr=0):
    assert L % n_matmul == 0 and L % period_eqlt == 0
    N = Nx * Ny

    if seed is None:
        seed = int(time.time())
    init_rng = rand_seed(seed)
    init_hs = np.zeros((L, N), dtype=np.int32)

    for l in range(L):
        for i in range(N):
            init_hs[l, i] = rand_uint(init_rng) >> np.uint64(63)

    # 1 site mapping
    map_i = np.zeros(N, dtype=np.int32)
    degen_i = np.array((N,), dtype=np.int32)
    num_i = map_i.max() + 1
    assert num_i == degen_i.size

    # 2 site mapping
    map_ij = np.zeros((N, N), dtype=np.int32)
    degen_ij = np.zeros(N, dtype=np.int32)
    for jy in range(Ny):
        for jx in range(Nx):
            for iy in range(Ny):
                for ix in range(Nx):
                    ky = (iy - jy) % Ny
                    kx = (ix - jx) % Nx
                    map_ij[jx + Nx*jy, ix + Nx*iy] = kx + Nx*ky
                    degen_ij[kx + Nx*ky] += 1
    num_ij = map_ij.max() + 1
    assert num_ij == degen_ij.size

    # bond definitions
    bps = 4 if tp != 0.0 else 2  # bonds per site
    num_b = bps*N  # total bonds in cluster
    bonds = np.zeros((2, num_b), dtype=np.int32)
    for iy in range(Ny):
        for ix in range(Nx):
            i = ix + Nx*iy
            iy1 = (iy + 1) % Ny
            ix1 = (ix + 1) % Nx
            bonds[0, i] = i            # i0 = i
            bonds[1, i] = ix1 + Nx*iy  # i1 = i + x
            bonds[0, i + N] = i            # i0 = i
            bonds[1, i + N] = ix + Nx*iy1  # i1 = i + y
            if bps == 4:
                bonds[0, i + 2*N] = i             # i0 = i
                bonds[1, i + 2*N] = ix1 + Nx*iy1  # i1 = i + x + y
                bonds[0, i + 3*N] = ix1 + Nx*iy   # i0 = i + x
                bonds[1, i + 3*N] = ix + Nx*iy1   # i1 = i + y

    # 1 bond 1 site mapping
    num_bs = bps*N
    map_bs = np.zeros((N, num_b), dtype=np.int32)
    degen_bs = np.zeros(num_bs, dtype=np.int32)
    for jy in range(Ny):
        for jx in range(Nx):
            for iy in range(Ny):
                for ix in range(Nx):
                    ky = (iy - jy) % Ny
                    kx = (ix - jx) % Nx
                    i = ix + Nx*iy
                    j = jx + Nx*jy
                    k = kx + Nx*ky
                    for ii in range(bps):
                        kk = k + N*ii
                        map_bs[j, i + N*ii] = kk
                        degen_bs[kk] += 1

    # 2 bond mapping
    num_bb = bps*bps*N
    map_bb = np.zeros((num_b, num_b), dtype=np.int32)
    degen_bb = np.zeros(num_bb, dtype = np.int32)
    for jy in range(Ny):
        for jx in range(Nx):
            for iy in range(Ny):
                for ix in range(Nx):
                    ky = (iy - jy) % Ny
                    kx = (ix - jx) % Nx
                    i = ix + Nx*iy
                    j = jx + Nx*jy
                    k = kx + Nx*ky
                    for jj in range(bps):
                        for ii in range(bps):
                            kk =  k + N*(ii + bps*jj)
                            map_bb[j + N*jj, i + N*ii] = kk
                            degen_bb[kk] += 1

    B = nflux/(Ny*Nx)
    tij = np.zeros((Ny*Nx, Ny*Nx), dtype=np.complex)
    for iy in range(Ny):
        for ix in range(Nx):
            iy1 = (iy + 1) % Ny
            ix1 = (ix + 1) % Nx
                #jx    jy    ix    iy
            tij[ix + Nx*iy1, ix + Nx*iy] += np.exp(1j*np.pi*B*( 1* ix + (0 if iy1 > iy else  Ny* ix)))
            tij[ix + Nx*iy, ix + Nx*iy1] += np.exp(1j*np.pi*B*(-1* ix + (0 if iy1 > iy else -Ny* ix)))
            tij[ix1 + Nx*iy, ix + Nx*iy] += np.exp(1j*np.pi*B*( 1*-iy + (0 if ix1 > ix else  Nx*-iy)))
            tij[ix + Nx*iy, ix1 + Nx*iy] += np.exp(1j*np.pi*B*(-1*-iy + (0 if ix1 > ix else -Nx*-iy)))

            tij[ix1 + Nx*iy1, ix + Nx*iy] += tp * np.exp(1j*np.pi*B*( 1*ix  +  1*-iy  + (0 if iy1 > iy else  Ny*ix1) + (0 if ix1 > ix else  Nx*-iy1) + (-Nx*Ny if ix1 < ix and iy1 < iy else 0)))
            tij[ix + Nx*iy, ix1 + Nx*iy1] += tp * np.exp(1j*np.pi*B*(-1*ix1 + -1*-iy1 + (0 if iy1 > iy else -Ny*ix ) + (0 if ix1 > ix else -Nx*-iy ) + ( Nx*Ny if ix1 < ix and iy1 < iy else 0)))
            tij[ix1 + Nx*iy, ix + Nx*iy1] += tp * np.exp(1j*np.pi*B*(-1*ix  +  1*-iy1 + (0 if iy1 > iy else -Ny*ix1) + (0 if ix1 > ix else  Nx*-iy ) + ( Nx*Ny if ix1 < ix and iy1 < iy else 0)))
            tij[ix + Nx*iy1, ix1 + Nx*iy] += tp * np.exp(1j*np.pi*B*( 1*ix1 + -1*-iy  + (0 if iy1 > iy else  Ny*ix ) + (0 if ix1 > ix else -Nx*-iy1) + (-Nx*Ny if ix1 < ix and iy1 < iy else 0)))
    Ku = -1*tij
    Kd = -1*tij
    for i in range(Ny*Nx):
        Ku[i, i] -= mu
        Kd[i, i] -= mu

    exp_Ku = expm(-dt * Ku)
    exp_Kd = expm(-dt * Kd)
    inv_exp_Ku = expm(dt * Ku)
    inv_exp_Kd = expm(dt * Kd)
    exp_halfKu = expm(-dt/2 * Ku)
    exp_halfKd = expm(-dt/2 * Kd)
    inv_exp_halfKu = expm(dt/2 * Ku)
    inv_exp_halfKd = expm(dt/2 * Kd)
#   exp_K = np.array(mpm.expm(mpm.matrix(-dt * K)).tolist(), dtype=np.float64)

    U_i = np.array((U,), dtype=np.float64)
    assert U_i.shape[0] == num_i

    exp_lmbd = np.exp(0.5*U_i*dt) + np.sqrt(np.expm1(U_i*dt))
#    exp_lmbd = np.exp(np.arccosh(np.exp(0.5*U_i*dt)))
#    exp_lmbd = float(mpm.exp(mpm.acosh(mpm.exp(0.5*float(U*dt)))))
    exp_lambda = np.array((1.0/exp_lmbd[map_i], exp_lmbd[map_i]))
    delll = np.array((exp_lmbd[map_i]**2 - 1, exp_lmbd[map_i]**-2 - 1))

    if filename is None:
        filename = "{}.h5".format(seed)
    with h5py.File(filename, "w" if overwrite else "x") as f:
        # parameters not used by dqmc code, but useful for analysis
        f.create_group("metadata")
        f["metadata"]["version"] = 0.0
        f["metadata"]["model"] = "Hubbard (complex)"
        f["metadata"]["Nx"] = Nx
        f["metadata"]["Ny"] = Ny
        f["metadata"]["bps"] = bps
        f["metadata"]["U"] = U
        f["metadata"]["t'"] = tp
        f["metadata"]["nflux"] = nflux
        f["metadata"]["mu"] = mu
        f["metadata"]["beta"] = L*dt

        # parameters used by dqmc code
        f.create_group("params")
        # model parameters
        f["params"]["N"] = np.array(N, dtype=np.int32)
        f["params"]["L"] = np.array(L, dtype=np.int32)
        f["params"]["map_i"] = map_i
        f["params"]["map_ij"] = map_ij
        f["params"]["bonds"] = bonds
        f["params"]["map_bs"] = map_bs
        f["params"]["map_bb"] = map_bb
        f["params"]["Ku"] = Ku
        f["params"]["Kd"] = Kd
        f["params"]["U"] = U_i
        f["params"]["dt"] = np.array(dt, dtype=np.float64)

        # simulation parameters
        f["params"]["n_matmul"] = np.array(n_matmul, dtype=np.int32)
        f["params"]["n_delay"] = np.array(n_delay, dtype=np.int32)
        f["params"]["n_sweep_warm"] = np.array(n_sweep_warm, dtype=np.int32)
        f["params"]["n_sweep_meas"] = np.array(n_sweep_meas, dtype=np.int32)
        f["params"]["period_eqlt"] = np.array(period_eqlt, dtype=np.int32)
        f["params"]["period_uneqlt"] = np.array(period_uneqlt, dtype=np.int32)
        f["params"]["meas_bond_corr"] = meas_bond_corr
        f["params"]["meas_energy_corr"] = meas_energy_corr
        f["params"]["meas_nematic_corr"] = meas_nematic_corr
        f["params"]["init_rng"] = init_rng  # save if need to replicate data

        # precalculated stuff
        f["params"]["num_i"] = num_i
        f["params"]["num_ij"] = num_ij
        f["params"]["num_b"] = num_b
        f["params"]["num_bs"] = num_bs
        f["params"]["num_bb"] = num_bb
        f["params"]["degen_i"] = degen_i
        f["params"]["degen_ij"] = degen_ij
        f["params"]["degen_bs"] = degen_bs
        f["params"]["degen_bb"] = degen_bb
        f["params"]["exp_Ku"] = exp_Ku
        f["params"]["exp_Kd"] = exp_Kd
        f["params"]["inv_exp_Ku"] = inv_exp_Ku
        f["params"]["inv_exp_Kd"] = inv_exp_Kd
        f["params"]["exp_halfKu"] = exp_halfKu
        f["params"]["exp_halfKd"] = exp_halfKd
        f["params"]["inv_exp_halfKu"] = inv_exp_halfKu
        f["params"]["inv_exp_halfKd"] = inv_exp_halfKd
        f["params"]["exp_lambda"] = exp_lambda
        f["params"]["del"] = delll
        f["params"]["F"] = np.array(L//n_matmul, dtype=np.int32)
        f["params"]["n_sweep"] = np.array(n_sweep_warm + n_sweep_meas,
                                          dtype=np.int32)

        # simulation state
        f.create_group("state")
        f["state"]["sweep"] = np.array(0, dtype=np.int32)
        f["state"]["rng"] = init_rng
        f["state"]["hs"] = init_hs

        # measurements
        f.create_group("meas_eqlt")
        f["meas_eqlt"]["n_sample"] = np.array(0, dtype=np.int32)
        f["meas_eqlt"]["sign"] = np.array(0.0, dtype=np.complex)
        f["meas_eqlt"]["density"] = np.zeros(num_i, dtype=np.complex)
        f["meas_eqlt"]["double_occ"] = np.zeros(num_i, dtype=np.complex)
        f["meas_eqlt"]["g00"] = np.zeros(num_ij, dtype=np.complex)
        f["meas_eqlt"]["nn"] = np.zeros(num_ij, dtype=np.complex)
        f["meas_eqlt"]["xx"] = np.zeros(num_ij, dtype=np.complex)
        f["meas_eqlt"]["zz"] = np.zeros(num_ij, dtype=np.complex)
        f["meas_eqlt"]["pair_sw"] = np.zeros(num_ij, dtype=np.complex)
        if meas_energy_corr:
            f["meas_eqlt"]["kk"] = np.zeros(num_bb, dtype=np.complex)
            f["meas_eqlt"]["kv"] = np.zeros(num_bs, dtype=np.complex)
            f["meas_eqlt"]["kn"] = np.zeros(num_bs, dtype=np.complex)
            f["meas_eqlt"]["vv"] = np.zeros(num_ij, dtype=np.complex)
            f["meas_eqlt"]["vn"] = np.zeros(num_ij, dtype=np.complex)

        if period_uneqlt > 0:
            f.create_group("meas_uneqlt")
            f["meas_uneqlt"]["n_sample"] = np.array(0, dtype=np.int32)
            f["meas_uneqlt"]["sign"] = np.array(0.0, dtype=np.complex)
            f["meas_uneqlt"]["gt0"] = np.zeros(num_ij*L, dtype=np.complex)
            f["meas_uneqlt"]["nn"] = np.zeros(num_ij*L, dtype=np.complex)
            f["meas_uneqlt"]["xx"] = np.zeros(num_ij*L, dtype=np.complex)
            f["meas_uneqlt"]["zz"] = np.zeros(num_ij*L, dtype=np.complex)
            f["meas_uneqlt"]["pair_sw"] = np.zeros(num_ij*L, dtype=np.complex)
            if meas_bond_corr:
                f["meas_uneqlt"]["pair_bb"] = np.zeros(num_bb*L, dtype=np.complex)
                f["meas_uneqlt"]["jj"] = np.zeros(num_bb*L, dtype=np.complex)
                f["meas_uneqlt"]["jsjs"] = np.zeros(num_bb*L, dtype=np.complex)
                f["meas_uneqlt"]["kk"] = np.zeros(num_bb*L, dtype=np.complex)
                f["meas_uneqlt"]["ksks"] = np.zeros(num_bb*L, dtype=np.complex)
            if meas_energy_corr:
                f["meas_uneqlt"]["kv"] = np.zeros(num_bs*L, dtype=np.complex)
                f["meas_uneqlt"]["kn"] = np.zeros(num_bs*L, dtype=np.complex)
                f["meas_uneqlt"]["vv"] = np.zeros(num_ij*L, dtype=np.complex)
                f["meas_uneqlt"]["vn"] = np.zeros(num_ij*L, dtype=np.complex)
            if meas_nematic_corr:
                f["meas_uneqlt"]["nem_nnnn"] = np.zeros(num_bb*L, dtype=np.complex)
                f["meas_uneqlt"]["nem_ssss"] = np.zeros(num_bb*L, dtype=np.complex)
    return filename


def create_batch(Nfiles=1, prefix=None, seed=None, **kwargs):
    if seed is None:
        seed = int(time.time())
    if prefix is None:
        prefix = str(seed)
    rng = rand_seed(seed)

    file_0 = "{}_{}.h5".format(prefix, 0)

    create_1(filename=file_0, seed=seed, **kwargs)
    with h5py.File(file_0, "r") as f:
        N = f["params"]["N"][...]
        L = f["params"]["L"][...]

    for i in range(1, Nfiles):
        rand_jump(rng)
        init_rng = rng.copy()
        init_hs = np.zeros((L, N), dtype=np.int32)

        for l in range(L):
            for r in range(N):
                init_hs[l, r] = rand_uint(init_rng) >> np.uint64(63)

        file_i = "{}_{}.h5".format(prefix, i)
        shutil.copy2(file_0, file_i)
        with h5py.File(file_i, "r+") as f:
            f["params"]["init_rng"][...] = init_rng
            f["state"]["rng"][...] = init_rng
            f["state"]["hs"][...] = init_hs
    return file_0 if Nfiles == 1 else "{} ... {}".format(file_0, file_i)


def main(argv):
    kwargs = {}
    for arg in argv[1:]:
        eq = arg.find("=")
        if eq == -1:
            print("couldn't find \"=\" in argument " + arg)
            return
        key = arg[:eq]
        val = arg[(eq + 1):]
        try:
            val = int(val)
        except ValueError:
            try:
                val = float(val)
            except:
                pass
        kwargs[key] = val
    print("created simulation files:", create_batch(**kwargs))

if __name__ == "__main__":
    main(sys.argv)