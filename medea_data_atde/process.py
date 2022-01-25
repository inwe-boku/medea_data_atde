# %% imports
import logging
from logging_config import setup_logging
import pandas as pd
import os
import numpy as np
from scipy import interpolate
from datetime import datetime as dt
from netCDF4 import Dataset, num2date
from medea_data_atde.retrieve import days_in_year, resample_index
# from config import MEDEA_ROOT_DIR, ERA_DIR, COUNTRY, YEARS, zones, imf_file, fx_file, co2_file, url_ageb_bal



# %% functions for heat load calculation
# ----------------------------------------------------------------------------------------------------------------------
def heat_yr2day(av_temp, ht_cons_annual):
    """
    Converts annual heat consumption to daily heat consumption, based on daily mean temperatures.
    Underlying algorithm relies on https://www.agcs.at/agcs/clearing/lastprofile/lp_studie2008.pdf
    Implemented consumer clusters are for residential and commercial consumers:
    * HE08 Heizgas Einfamilienhaus LP2008
    * MH08 Heizgas Mehrfamilienhaus LP2008
    * HG08 Heizgas Gewerbe LP2008
    Industry load profiles are specific and typically measured, i.e. not approximated by load profiles

    :param av_temp: datetime-indexed pandas.DataFrame holding daily average temperatures
    :param ht_cons_annual:
    :return:
    """
    # ----------------------------------------------------------------------------
    # fixed parameter: SIGMOID PARAMETERS
    # ----------------------------------------------------------------------------
    sigm_a = {'HE08': 2.8423015098, 'HM08': 2.3994211316, 'HG08': 3.0404658371}
    sigm_b = {'HE08': -36.9902101066, 'HM08': -34.1350545407, 'HG08': -35.6696458089}
    sigm_c = {'HE08': 6.5692076687, 'HM08': 5.6347421440, 'HG08': 5.6585923962}
    sigm_d = {'HE08': 0.1225658254, 'HM08': 0.1728484079, 'HG08': 0.1187586955}

    # ----------------------------------------------------------------------------
    # breakdown of annual consumption to daily consumption
    # ----------------------------------------------------------------------------
    # temperature smoothing
    temp_smooth = pd.DataFrame(index=av_temp.index, columns=['Temp_Sm'])
    for d in av_temp.index:
        if d >= av_temp.first_valid_index() + pd.Timedelta(1, unit='d'):
            temp_smooth.loc[d] = 0.5 * av_temp.loc[d] \
                                 + 0.5 * temp_smooth.loc[d - pd.Timedelta(1, unit='d')]
        else:
            temp_smooth.loc[d] = av_temp.loc[d]

    # determination of normalized daily consumption h_value
    h_value = pd.DataFrame(index=av_temp.index, columns=sigm_a.keys())
    for key in sigm_a:
        h_value[key] = sigm_a[key] / (1 + (sigm_b[key] / (temp_smooth.values - 40)) ** sigm_c[key]) + sigm_d[key]

    # generate matrix of hourly annual consumption
    annual_hourly_consumption = pd.DataFrame(1, index=av_temp.index, columns=sigm_a.keys())
    annual_hourly_consumption = annual_hourly_consumption.set_index(annual_hourly_consumption.index.year, append=True)
    annual_hourly_consumption = annual_hourly_consumption.multiply(ht_cons_annual, level=1).reset_index(drop=True,
                                                                                                        level=1)

    # generate matrix of annual h-value sums
    h_value_annual = h_value.groupby(h_value.index.year).sum()

    # de-normalization of h_value
    cons_daily = h_value.multiply(annual_hourly_consumption)
    cons_daily = cons_daily.set_index(cons_daily.index.year, append=True)
    cons_daily = cons_daily.divide(h_value_annual, level=1).reset_index(drop=True, level=1)

    return cons_daily


def heat_day2hr(df_ht, con_day, con_pattern):
    """
    convert daily heat consumption to hourly heat consumption
    Underlying algorithm relies on https://www.agcs.at/agcs/clearing/lastprofile/lp_studie2008.pdf
    ATTENTION: Algorithm fails for daily average temperatures below -25°C !
    :param df_ht:
    :param con_day:
    :param con_pattern:
    :return:
    """
    sigm_a = {'HE08': 2.8423015098, 'HM08': 2.3994211316, 'HG08': 3.0404658371}
    # apply demand_pattern
    last_day = pd.DataFrame(index=df_ht.tail(1).index + pd.Timedelta(1, unit='d'), columns=sigm_a.keys())

    cons_hourly = con_day.append(last_day).astype(float).resample('1H').sum()
    cons_hourly.drop(cons_hourly.tail(1).index, inplace=True)

    for d in df_ht.index:
        temp_lvl = np.floor(df_ht[d] / 5) * 5
        cons_hlpr = con_day.loc[d] * con_pattern.loc[temp_lvl]
        cons_hlpr = cons_hlpr[cons_hourly.columns]
        cons_hlpr.index = d + pd.to_timedelta(cons_hlpr.index, unit='h')
        cons_hourly.loc[cons_hlpr.index] = cons_hlpr.astype(str).astype(float)

    cons_hourly = cons_hourly.astype(str).astype(float)
    return cons_hourly


def mean_temp_at_plants(db_plants, era_dir, country, years, zones):
    temp_date_range = pd.date_range(pd.datetime(years[0], 1, 1), pd.datetime(years[-1], 12, 31), freq='D')
    daily_mean_temp = pd.DataFrame(index=temp_date_range.values, columns=[zones])
    for zne in zones:
        chp = db_plants[(db_plants['UnitCoGen'] == 1) & (db_plants['UnitNameplate'] >= 10) &
                        (db_plants['PlantCountry'] == country[zne])]
        chp_lon = chp['PlantLongitude'].values
        chp_lat = chp['PlantLatitude'].values
        for year in years:
            filename = os.path.join(era_dir, f'temperature_europe_{year}.nc')
            era5 = Dataset(filename, format='NETCDF4')
            # get grid
            lats = era5.variables['latitude'][:]  # y
            lons = era5.variables['longitude'][:]  # x
            DAYS = range(0, days_in_year(year), 1)
            for day in DAYS:
                hour = day * 24
                temp_2m = era5.variables['t2m'][hour, :, :] - 273.15
                # obtain weighted average temperature from interpolation function, using CHP capacities as weights
                f = interpolate.interp2d(lons, lats, temp_2m)
                temp_itp = np.diagonal(f(chp_lon, chp_lat)) * chp['UnitNameplate'].values / chp['UnitNameplate'].sum()
                era_date = num2date(era5.variables['time'][hour], era5.variables['time'].units,
                                    era5.variables['time'].calendar)
                # conversion of date formats
                era_date = dt.fromisoformat(era_date.isoformat())
                daily_mean_temp.loc[era_date, zne] = np.nansum(temp_itp)
                # print(era_date)
    # export results
    daily_mean_temp.replace(-9999, np.nan, inplace=True)
    return daily_mean_temp


def heat_consumption(zones, years, cons_annual, df_heat, cons_pattern):
    # ----------------------------------------------------------------------------
    dayrange = pd.date_range(pd.datetime(np.min(df_heat['year']), 1, 1), pd.datetime(np.max(df_heat['year']), 12, 31),
                             freq='D')

    # calculate heat consumption for each region
    # ----------------------------------------------------------------------------
    idx = pd.IndexSlice
    regions = zones
    ht_consumption = pd.DataFrame(index=resample_index(df_heat.index, 'h'), columns=cons_annual.columns)
    for reg in regions:
        cons_daily = heat_yr2day(df_heat[reg], cons_annual.loc[:, reg])
        cons_hourly = heat_day2hr(df_heat[reg], cons_daily, cons_pattern)
        cons_hourly.columns = pd.MultiIndex.from_product([[reg], cons_hourly.columns])
        ht_consumption.loc[:, idx[reg, :]] = cons_hourly

    # fill WW and IND
    for yr in years:
        for zn in zones:
            hrs = len(ht_consumption.loc[str(yr), :])
            ht_consumption.loc[str(yr), idx[zn, 'WW']] = cons_annual.loc[yr, idx[zn, 'WW']] / hrs
            ht_consumption.loc[str(yr), idx[zn, 'IND']] = cons_annual.loc[yr, idx[zn, 'IND']] / hrs

    return ht_consumption


def do_processing(medea_root_dir, country, years, zones, url_ageb_bal):
    setup_logging()

    # %% file paths
    ERA_DIR = medea_root_dir / 'data' / 'raw' / 'era5'
    imf_file = medea_root_dir / 'data' / 'raw' / 'imf_price_data.xlsx'
    fx_file = medea_root_dir / 'data' / 'raw' / 'ecb_fx_data.csv'
    co2_file = medea_root_dir / 'data' / 'raw' / 'eua_price.csv'
    enbal_at = medea_root_dir / 'data' / 'raw' / 'enbal_AT.xlsx'
    CONSUMPTION_PATTERN = medea_root_dir / 'data' / 'raw' / 'consumption_pattern.xlsx'

    fuel_price_file = medea_root_dir / 'data' / 'processed' / 'monthly_fuel_prices.csv'
    co2_price_file = medea_root_dir / 'data' / 'processed' / 'co2_price.csv'
    PPLANT_DB = medea_root_dir / 'data' / 'processed' / 'power_plant_db.xlsx'
    MEAN_TEMP_FILE = medea_root_dir / 'data' / 'processed' / 'temp_daily_mean.csv'
    heat_cons_file = medea_root_dir / 'data' / 'processed' / 'heat_hourly_consumption.csv'

    # %% process PRICE data
    df_imf = pd.read_excel(imf_file, index_col=[0], skiprows=[1, 2, 3])
    df_imf.index = pd.to_datetime(df_imf.index, format='%YM%m')

    df_fx = pd.read_csv(fx_file, index_col=[0], skiprows=[0, 2, 3, 4, 5], usecols=[1], na_values=['-']).astype('float')
    df_fx.index = pd.to_datetime(df_fx.index, format='%Y-%m-%d')

    # convert prices to EUR per MWh
    df_prices_mwh = pd.DataFrame(index=df_imf.index, columns=['USD_EUR', 'Brent_UK', 'Coal_SA', 'NGas_DE'])
    df_prices_mwh['USD_EUR'] = df_fx.resample('MS').mean()
    df_prices_mwh['Brent_UK'] = df_imf['POILBRE'] / df_prices_mwh['USD_EUR'] * 7.52 / 11.63
    df_prices_mwh['Coal_SA'] = df_imf['PCOALSA_USD'] / df_prices_mwh['USD_EUR'] / 6.97333
    df_prices_mwh['NGas_DE'] = df_imf['PNGASEU'] / df_prices_mwh['USD_EUR'] / 0.29307
    # drop rows with all nan
    df_prices_mwh.dropna(how='all', inplace=True)

    df_prices_mwh.to_csv(fuel_price_file)
    logging.info(f'fuel prices processed and saved to {fuel_price_file}')

    df_price_co2 = pd.read_csv(co2_file, index_col=[0])
    df_price_co2.index = pd.to_datetime(df_price_co2.index, format='%Y-%m-%d')
    df_price_co2['Settle'].to_csv(co2_price_file)
    logging.info(f'CO2 prices processed and saved to {co2_price_file}')

    # %% process temperature data
    # get coordinates of co-gen plants
    db_plants = pd.read_excel(PPLANT_DB)
    daily_mean_temp = mean_temp_at_plants(db_plants, ERA_DIR, country, years, zones)
    daily_mean_temp.to_csv(MEAN_TEMP_FILE)
    logging.info(f'Temperatures processed and saved to {MEAN_TEMP_FILE}')

    # %% process HEAT LOAD
    # process German energy balances
    ht_enduse_de = pd.DataFrame()
    for yr in [x - 2000 for x in years]:
        enebal_de = medea_root_dir / 'data' / 'raw' / f'enbal_DE_20{yr}.{url_ageb_bal[yr][1]}'
        df = pd.read_excel(enebal_de, sheet_name='tj', index_col=[0], usecols=[0, 31], skiprows=list(range(0, 50)),
                           nrows=24, na_values=['-'])
        df.columns = [2000 + yr]
        ht_enduse_de = pd.concat([ht_enduse_de, df], axis=1)
    ht_enduse_de = ht_enduse_de / 3.6

    # process Austrian energy balances
    ht_enduse_at = pd.read_excel(enbal_at, sheet_name='Fernwärme', header=[438], index_col=[0], nrows=24,
                                 na_values=['-']).astype('float')
    ht_enduse_at = ht_enduse_at / 1000

    ht_cons = pd.DataFrame(index=years,
                           columns=pd.MultiIndex.from_product([zones, ['HE08', 'HM08', 'HG08', 'WW', 'IND']]))
    ht_cons.loc[years, ('AT', 'HE08')] = ht_enduse_at.loc['Private Haushalte', years] * 0.376 * 0.75
    ht_cons.loc[years, ('AT', 'HM08')] = ht_enduse_at.loc['Private Haushalte', years] * 0.624 * 0.75
    ht_cons.loc[years, ('AT', 'WW')] = ht_enduse_at.loc['Private Haushalte', years] * 0.25
    ht_cons.loc[years, ('AT', 'HG08')] = ht_enduse_at.loc['Öffentliche und Private Dienstleistungen', years]
    ht_cons.loc[years, ('AT', 'IND')] = ht_enduse_at.loc['Produzierender Bereich', years]
    ht_cons.loc[years, ('DE', 'HE08')] = ht_enduse_de.loc['Haushalte', years] * 0.376 * 0.75
    ht_cons.loc[years, ('DE', 'HM08')] = ht_enduse_de.loc['Haushalte', years] * 0.624 * 0.75
    ht_cons.loc[years, ('DE', 'WW')] = ht_enduse_de.loc['Haushalte', years] * 0.25
    ht_cons.loc[years, ('DE', 'HG08')] = ht_enduse_de.loc[
        'Gewerbe, Handel, Dienstleistungen u.übrige Verbraucher', years]
    ht_cons.loc[years, ('DE', 'IND')] = ht_enduse_de.loc[
        'Bergbau, Gew. Steine u. Erden, Verarbeit. Gewerbe insg.', years]

    """ 
    Above transformations are based on following Assumptions
     * share of heating energy used for hot water preparation: 25%
       (cf. https://www.umweltbundesamt.at/fileadmin/site/publikationen/REP0074.pdf, p. 98)
     * share of heat delivered to single-family homes:
       - in Austria 2/3 of houses are single-family houses, which are home to 40% of the population
       - in Germany, 65.1% of houses are EFH, 17.2% are ZFH and 17.7% are MFH
         (cf. https://www.statistik.rlp.de/fileadmin/dokumente/gemeinschaftsveroeff/zen/Zensus_GWZ_2014.pdf)
       - average space in single-family houses: 127.3 m^2; in multiple dwellings: 70.6 m^2
       - specific heat consumption: EFH: 147.9 kWh/m^2; MFH: 126.5 kWh/m^2
         (cf. http://www.rwi-essen.de/media/content/pages/publikationen/rwi-projektberichte/
         PB_Datenauswertung-Energieverbrauch-privHH.pdf, p. 5)
       - implied share of heat consumption, assuming on average 7 appartments per MFH: 37.6% EFH; 62.4% MFH
    """
    # Accounting for own consumption and pipe losses
    ht_cons = ht_cons.multiply(1.125)

    df_heat = pd.read_csv(MEAN_TEMP_FILE, index_col=[0], parse_dates=True)
    df_heat['year'] = df_heat.index.year
    df_heat['weekday'] = df_heat.index.strftime('%a')
    df_heat.fillna(method='pad', inplace=True)  # fill NA to prevent NAs in temperature smoothing below

    cons_pattern = pd.read_excel(CONSUMPTION_PATTERN, 'consumption_pattern', index_col=[0, 1])
    cons_pattern = cons_pattern.rename_axis('hour', axis=1)
    cons_pattern = cons_pattern.unstack('consumer').stack('hour')

    ht_consumption = heat_consumption(zones, years, ht_cons, df_heat, cons_pattern)
    ht_consumption.to_csv(heat_cons_file)
    logging.info(f'exported hourly heat demand to {heat_cons_file}')
