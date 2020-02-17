#!/usr/bin/env python
import os
import stat
import sys
import numpy as np
import scipy.signal
from astropy import units as u
from astropy.io import fits
from spectral_cube import SpectralCube
from radio_beam import Beam, Beams
from glob import glob
import schwimmbad
import multiprocessing as mp
import ctypes as c
from tqdm import tqdm, trange
import au2
import functools
from mpi4py import MPI
print = functools.partial(print, flush=True)


class Error(OSError):
    pass


class SameFileError(Error):
    """Raised when source and destination are the same file."""


class SpecialFileError(OSError):
    """Raised when trying to do a kind of operation (e.g. copying) which is
    not supported on a special file (e.g. a named pipe)"""


class ExecError(OSError):
    """Raised when a command could not be executed"""


class ReadError(OSError):
    """Raised when an archive cannot be read"""


class RegistryError(Exception):
    """Raised when a registry operation with the archiving
    and unpacking registeries fails"""


def _samefile(src, dst):
    # Macintosh, Unix.
    if hasattr(os.path, 'samefile'):
        try:
            return os.path.samefile(src, dst)
        except OSError:
            return False


def copyfile(src, dst, *, follow_symlinks=True, verbose=True):
    """Copy data from src to dst.

    If follow_symlinks is not set and src is a symbolic link, a new
    symlink will be created instead of copying the file it points to.

    """
    if _samefile(src, dst):
        raise SameFileError("{!r} and {!r} are the same file".format(src, dst))

    for fn in [src, dst]:
        try:
            st = os.stat(fn)
        except OSError:
            # File most likely does not exist
            pass
        else:
            # XXX What about other special files? (sockets, devices...)
            if stat.S_ISFIFO(st.st_mode):
                raise SpecialFileError("`%s` is a named pipe" % fn)

    if not follow_symlinks and os.path.islink(src):
        os.symlink(os.readlink(src), dst)
    else:
        with open(src, 'rb') as fsrc:
            with open(dst, 'wb') as fdst:
                copyfileobj(fsrc, fdst, verbose=verbose)
    return dst


def copyfileobj(fsrc, fdst, length=16*1024, verbose=True):
    #copied = 0
    total = os.fstat(fsrc.fileno()).st_size
    with tqdm(
            total=total,
            disable=(not verbose),
            unit_scale=True,
            desc='Copying file'
    ) as pbar:
        while True:
            buf = fsrc.read(length)
            if not buf:
                break
            fdst.write(buf)
            copied = len(buf)
            pbar.update(copied)


def round_up(n, decimals=0):
    multiplier = 10 ** decimals
    return np.ceil(n * multiplier) / multiplier


def getbeams(beamlog, verbose=False):
    """

    colnames=['Channel', 'BMAJarcsec', 'BMINarcsec', 'BPAdeg']
    """
    if verbose:
        print(f'Getting beams from {beamlog}')
    beams = np.genfromtxt(beamlog, names=True)
    nchan = beams.shape[0]

    return beams, nchan


def getfacs(datadict, new_beam, verbose=False):
    """Get beam info
    """
    conbms = []
    facs = []
    for oldbeam in datadict['oldbeams']:
        if verbose:
            print(f"Current beam is", oldbeam)
        conbm = new_beam.deconvolve(oldbeam)
        fac, amp, outbmaj, outbmin, outbpa = au2.gauss_factor(
            [
                conbm.major.to(u.arcsec).value,
                conbm.minor.to(u.arcsec).value,
                conbm.pa.to(u.deg).value
            ],
            beamOrig=[
                oldbeam.major.to(u.arcsec).value,
                oldbeam.minor.to(u.arcsec).value,
                oldbeam.pa.to(u.deg).value
            ],
            dx1=datadict['dx'].to(u.arcsec).value,
            dy1=datadict['dy'].to(u.arcsec).value
        )
        conbms.append(conbm)
        facs.append(fac)
    return conbms, facs


def smooth(image, dy, conbeam, sfactor, verbose=False):
    """Do the smoothing
    """
    if np.isnan(conbeam):
        return image*np.nan
    else:
    # using Beams package
        if verbose:
            print(f'Using convolving beam', conbeam)
        pix_scale = dy
        gauss_kern = conbeam.as_kernel(dy)

        conbm1 = gauss_kern.array/gauss_kern.array.max()
        newim = scipy.signal.convolve(
            image.astype('f8'), conbm1, mode='same')
    newim *= sfactor
    return newim


def cpu_to_use(max_cpu, count):
    """Find number of cpus to use.
    Find the right number of cpus to use when dividing up a task, such
    that there are no remainders.
    Args:
        max_cpu (int): Maximum number of cores to use for a process.
        count (float): Number of tasks.

    Returns:
        Maximum number of cores to be used that divides into the number
        of tasks (int).
    """
    factors = []
    for i in range(1, count + 1):
        if count % i == 0:
            factors.append(i)
    factors = np.array(factors)
    return max(factors[factors <= max_cpu])


def worker(idx, start):
    newim = smooth(arr_in[idx], cubedict['dy'], cubedict['conbeams']
                   [start+idx], cubedict['sfactors'][start+idx], verbose=False)
    arr_out[idx] = newim[:]


def init_worker():
    # use global variables.
    global arr_in
    global arr_out
    global cubedict


def main(args, verbose=True):
    # Fix up outdir
    if args.mpi:
        pool = schwimmbad.MPIPool()

        if not pool.is_master():
            pool.wait()
            sys.exit(0)

        mpiComm = MPI.COMM_WORLD
        n_cores = mpiComm.Get_size()
        #mpiRank = mpiComm.Get_rank()
    outdir = args.outdir
    if outdir is not None:
        if outdir[-1] == '/':
            outdir = outdir[:-1]
    else:
        outdir = '.'

    files = glob(args.infile)
    if files == []:
        raise Exception('No files found!')

    beams = []
    nchans = []
    datadict = {}
    masks = []
    for i, file in enumerate(files):
        # Set up files
        datadict[f"cube_{i}"] = {}
        datadict[f"cube_{i}"]["filename"] = file
        # Get metadata
        header = fits.getheader(file)
        dxas = header['CDELT1']*-1*u.deg
        datadict[f"cube_{i}"]["dx"] = dxas
        dyas = header['CDELT2']*u.deg
        datadict[f"cube_{i}"]["dy"] = dyas
        # Get beam info
        beamlog = f"beamlog.{file}".replace('.fits', '.txt')
        datadict[f"cube_{i}"]["beamlog"] = beamlog
        beam, nchan = getbeams(beamlog, verbose=verbose)
        # Find bad chans
        cube = SpectralCube.read(file)
        mask = cube[:, cube.shape[1]//2, cube.shape[2]//2].mask.view()
        masks.append(mask)
        # Record beams
        beams.append(beam)
        nchans.append(nchan)
    beams = np.array(beams)
    nchans = np.array(nchans)
    # Do dome masking
    totalmask = sum(masks) > 0

    for i, _ in enumerate(beams['BMAJarcsec']):
        beams['BMAJarcsec'][i][totalmask] = np.nan
        beams['BMINarcsec'][i][totalmask] = np.nan
        beams['BPAdeg'][i][totalmask] = np.nan
        datadict[f"cube_{i}"]["oldbeams"] = Beams(beams['BMAJarcsec'][i].ravel(
        )*u.arcsec, beams['BMINarcsec'][i].ravel()*u.arcsec, beams['BPAdeg'][i].ravel()*u.deg)

    if not all(elem == nchans[0] for elem in nchans):
        raise Exception('Unequal channel count in beamlogs!')

    beamlst = Beams(beams['BMAJarcsec'].ravel(
    )*u.arcsec, beams['BMINarcsec'].ravel()*u.arcsec, beams['BPAdeg'].ravel()*u.deg)

    big_beam = beamlst.largest_beam()
    if verbose:
        print(f'largest beam is', big_beam)
    # Parse args
    bmaj = args.bmaj
    bmin = args.bmin
    bpa = args.bpa

    # Set to largest
    if bpa is None and bmin is not None and bmaj is not None:
        bpa = big_beam.bpa.to(u.deg)
    else:
        bpa = 0*u.deg
    if bmaj is None:
        bmaj = round_up(big_beam.major.to(u.arcsec))
    elif bmaj*u.arcsec < round_up(big_beam.major.to(u.arcsec)):
        raise Exception('Selected BMAJ is too small!')
    else:
        bmaj *= u.arcsec
    if bmin is None:
        bmin = round_up(big_beam.major.to(u.arcsec))
    elif bmin*u.arcsec < round_up(big_beam.major.to(u.arcsec)):
        raise Exception('Selected BMIN is too small!')
    else:
        bmin *= u.arcsec

    new_beam = Beam(
        bmaj,
        bmin,
        bpa
    )
    if verbose:
        print(f'Final beam is', new_beam)

    for key in tqdm(datadict.keys(), desc='Working on cubes separately'):
        conbms, facs = getfacs(datadict[key], new_beam, verbose=False)
        cube = SpectralCube.read(datadict[key]["filename"])
        # Set up output file
        outname = os.path.basename(
            datadict[key]["filename"].replace('.fits', '.sm.fits'))
        outfile = f'{outdir}/{outname}'
        if verbose:
            print(f'Initialsing to {outfile}')
        copyfile(datadict[key]["filename"], outfile, verbose=True)

        global cubedict
        cubedict = datadict[key]
        cubedict["conbeams"] = conbms
        cubedict["sfactors"] = facs

        if not args.mpi:
            n_cores = args.n_cores
        width_max = n_cores
        width = cpu_to_use(width_max, cube.shape[0])
        n_chunks = cube.shape[0]//width

        global arr_in
        global arr_out

        outshape = list(cube.shape)
        outshape[0] = width

        # shared, can be used from multiple processes
        mp_arr_out = mp.Array(c.c_double, int(np.prod(outshape)))
        # then in each new process create a new numpy array using:cp
        # mp_arr and arr share the same memory
        buff_arr_out = np.frombuffer(mp_arr_out.get_obj())
        # make it two-dimensional
        # b and arr share the same memory
        arr_out = buff_arr_out.reshape(outshape)
        arr_out[:] = np.zeros(outshape)*np.nan

        inshape = list(cube.shape)
        inshape[0] = width

        mp_arr_in = mp.Array(c.c_double, int(np.prod(inshape)))
        # then in each new process create a new numpy array using:
        # mp_arr and arr share the same memory
        buff_arr_in = np.frombuffer(mp_arr_in.get_obj())
        # make it two-dimensional
        # b and arr share the same memory
        arr_in = buff_arr_in.reshape(inshape)
        arr_in[:] = np.zeros(inshape)*np.nan

        for i in trange(
                n_chunks, disable=(not verbose),
                desc='Smoothing in chunks'
        ):
            start = i*width
            stop = start+width
            # if verbose:
            #print('Copying chunk of data to memory...')
            arr_in[:] = cube[start:stop, :]

            pool = schwimmbad.choose_pool(
                mpi=args.mpi, processes=args.n_cores, initializer=init_worker)
            if args.mpi:
                if not pool.is_master():
                    pool.wait()
                    sys.exit(0)
            func = functools.partial(worker, start=start)
            list(pool.map(func, [idx for idx in range(width)]))
            pool.close()

            with fits.open(outfile, mode='update', memmap=True) as outfh:
                outfh[0].data[start:stop,0,:,:] = arr_out[:]
                outfh.flush()

        if verbose:
            print('Updating header...')
        with fits.open(outfile, mode='update', memmap=True) as outfh:
                outfh[0].header = new_beam.attach_to_header(outfh[0].header)
                outfh.flush()
        # print(arr_out)


def cli():
    """Command-line interface
    """
    import argparse

    # Help string to be shown using the -h option
    descStr = """
    Smooth a field of 2D images to a common resolution.

    Names of output files are 'infile'.sm.fits

    """

    # Parse the command line options
    parser = argparse.ArgumentParser(description=descStr,
                                     formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument(
        'infile',
        metavar='infile',
        type=str,
        help='Input FITS image to smooth (can be a wildcard) - beam info must be in header.')

    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true",
                        help="verbose output [False].")

    parser.add_argument(
        '-o',
        '--outdir',
        dest='outdir',
        type=str,
        default=None,
        help='Output directory of smoothed FITS image(s) [./].')

    parser.add_argument(
        "--bmaj",
        dest="bmaj",
        type=float,
        default=None,
        help="BMAJ to convolve to [max BMAJ from given image(s)].")

    parser.add_argument(
        "--bmin",
        dest="bmin",
        type=float,
        default=None,
        help="BMIN to convolve to [max BMAJ from given image(s)].")

    parser.add_argument(
        "--bpa",
        dest="bpa",
        type=float,
        default=None,
        help="BPA to convolve to [0].")

    group = parser.add_mutually_exclusive_group()

    group.add_argument("--ncores", dest="n_cores", default=1,
                       type=int, help="Number of processes (uses multiprocessing).")
    group.add_argument("--mpi", dest="mpi", default=False,
                       action="store_true", help="Run with MPI.")

    args = parser.parse_args()

    verbose = args.verbose

    main(args, verbose=verbose)


if __name__ == "__main__":
    cli()