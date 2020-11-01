import numpy as np
import pandas as pd
import pandas.io.sql as sqlio
from datetime import datetime
from sklearn.model_selection import train_test_split
import psycopg2 as psql

from sql_queries import *


def connect():
    """
    Connects to the local database.
    """
    try:
        conn = psql.connect(dbname="mimic", user="postgres", password="postgres", host="localhost", )

        conn.cursor().execute("SET search_path TO mimiciii")
    except (Exception, psql.DatabaseError) as error:
        print(error)

    return conn


def create_aux_tables(conn):
    """
    Create auxiliary tables.
    """
    with conn:
        with conn.cursor() as cursor:
            query = ''.join(
                open('sql/cohort.sql').readlines())
            cursor.execute(query)
            query = ''.join(
                open('sql/easychart.sql').readlines())
            cursor.execute(query)
            query = ''.join(
                open('sql/easylabs.sql').readlines())
            cursor.execute(query)
            cursor.execute(query)
            query = ''.join(
                open('sql/easyvent.sql').readlines())
            cursor.execute(query)
            query = ''.join(
                open('sql/comorbidities.sql').readlines())
            cursor.execute(query)


def fill_missing_days(longitudinal):
    """
    Fill in missing days with in the ICU stay for each patient.
    """

    def f(x):
        days = set(x['ic_t'])
        l = len(x)
        missing = set(np.arange(l, dtype=np.float)) - days
        hadm_id = x['hadm_id'].iloc[0]
        icustay_id = x['icustay_id'].iloc[0]
        subject_id = x['subject_id'].iloc[0]
        for d in missing:
            x = x.append({'hadm_id': hadm_id, 'icustay_id': icustay_id,
                          'subject_id': subject_id, 'ic_t': d},
                         ignore_index=True)
        return x

    return longitudinal.groupby(['icustay_id']).apply(f).reset_index(
        drop=True)


def get_longitudinal_features(conn):
    """Extract longitudinal features from database connection conn."""
    longitudinal = sqlio.read_sql_query(query_longitudinal, conn)
    longitudinal = fill_missing_days(longitudinal)
    longitudinal['subject_id'] = longitudinal['subject_id'].astype('int64')
    longitudinal['hadm_id'] = longitudinal['hadm_id'].astype('int64')
    longitudinal['icustay_id'] = longitudinal['icustay_id'].astype('int64')
    longitudinal['ic_t'] = longitudinal['ic_t'].astype('int64')
    return longitudinal


def get_antibiotics_data(conn):
    """Extract antibiotic treatments from database connection conn."""
    abx_iv = sqlio.read_sql_query(query_antibiotics + 'select * from ab_tbl', conn)
    abx_iv_all = abx_iv[~abx_iv['antibiotic_name'].isnull()]

    patients_antibiotics = abx_iv_all
    patients_antibiotics['intime'] = patients_antibiotics['intime'].apply(
        lambda x: datetime.strptime(str(x), '%Y-%m-%d %H:%M:%S'))
    patients_antibiotics['antibiotic_time'] = (
        patients_antibiotics['antibiotic_time']
            .apply(lambda x: datetime.strptime(str(x), '%Y-%m-%d %H:%M:%S'))
    )
    patients_antibiotics['ic_t'] = (patients_antibiotics['antibiotic_time']
                                    - patients_antibiotics['intime'])
    patients_antibiotics['ic_t'] = patients_antibiotics['ic_t'].apply(
        lambda x: (x.days))

    def g(x):
        antibiotics = set(x['antibiotic_name'])
        x = x.head(n=1)
        x['antibiotic_name'].iloc[0] = antibiotics

        return x

    patients_antibiotics = patients_antibiotics.groupby(['icustay_id', 'ic_t']).apply(g).reset_index(drop=True)
    patients_antibiotics = patients_antibiotics.drop(columns=['intime', 'antibiotic_time'])
    patients_antibiotics = patients_antibiotics.rename(columns={'antibiotic_name': 'antibiotics'})

    return patients_antibiotics


def get_ventilator(conn):
    """Extract ventilator treatment from database connection conn."""
    vent = sqlio.read_sql_query(query_ventilator, conn)
    return vent


def get_static_features(conn):
    """Extract static features from database connection conn."""
    static_features = sqlio.read_sql_query(query_static_features, conn)
    static_features = static_features.drop(columns=['diagnosis', 'ethnicity', 'admission_type'])
    height_weight = sqlio.read_sql_query(query_height_weight, conn)
    comorbidities = sqlio.read_sql_query(query_comorbidities, conn)
    static_features = static_features.merge(height_weight, on=['subject_id', 'icustay_id'], how='outer')
    static_features = static_features.merge(comorbidities, on=['subject_id', 'hadm_id'], how='outer')
    return static_features


def extract_antibiotics_dataset():
    """Extract dataset from MIMIC consisting of patients receiving antibiotics."""
    print("Start creating SQL tables.")
    conn = connect()
    create_aux_tables(conn)
    print("Created SQL tables.")

    # Get the various data frames
    longitudinal = get_longitudinal_features(conn)
    antibiotics = get_antibiotics_data(conn)
    ventilator = get_ventilator(conn)
    static_features = get_static_features(conn)

    print("Extracted data from database.")

    # Filter by icustay_id
    icustay_ids = (set(antibiotics['icustay_id']) & set(longitudinal['icustay_id']))
    longitudinal = longitudinal[longitudinal['icustay_id'].isin(icustay_ids)]
    antibiotics = antibiotics[antibiotics['icustay_id'].isin(icustay_ids)]
    ventilator = ventilator[ventilator['icustay_id'].isin(icustay_ids)]
    static_features = static_features[static_features['icustay_id'].isin(icustay_ids)]

    # Combine the data frames
    merged_dataset = pd.merge(antibiotics, longitudinal, how='outer',
                              left_on=['icustay_id', 'hadm_id', 'ic_t'], right_on=['icustay_id', 'hadm_id', 'ic_t'])
    merged_dataset = pd.merge(merged_dataset, ventilator, how='outer',
                              left_on=['icustay_id', 'ic_t'], right_on=['icustay_id', 'ic_t'])
    merged_dataset = merged_dataset.sort_values(by=['icustay_id', 'ic_t']).reset_index(drop=True)

    # Some pre-processing
    merged_dataset['antibiotics'] = (~merged_dataset['antibiotics'].isnull()).astype('int64')
    merged_dataset[['mechvent', 'oxygentherapy']] = merged_dataset[['mechvent', 'oxygentherapy']].fillna(value=0.0)
    merged_dataset[['wbc']] = merged_dataset[['wbc']].fillna(method='ffill')

    merged_dataset = merged_dataset.drop(columns=['hadm_id', 'subject_id'])
    merged_dataset = merged_dataset.rename(columns={'icustay_id': 'id', 'ic_t': 'time', 'mechvent': 'ventilator'})
    static_features = static_features.drop(columns=['hadm_id', 'subject_id'])
    static_features = static_features.rename(columns={'icustay_id': 'id'})

    # Convert to long format and save
    longitudinal_dataset = pd.melt(merged_dataset, id_vars=['id', 'time'],
                                   value_vars=['antibiotics', 'heartratehigh', 'sysbp', 'diasbp',
                                               'meanbp', 'resprate', 'temperature', 'glucosechart', 'spo2', 'fio2',
                                               'bicarbonate', 'creatinine', 'chloride', 'glucoselab', 'hematocrit',
                                               'hemoglobin', 'platelet', 'potassium', 'ptt', 'inr', 'pt', 'sodium',
                                               'bun', 'wbc',
                                               'oxygentherapy', 'ventilator', 'extubated']).sort_values(
        by=['id', 'time']).reset_index(drop=True)

    indx = np.array(list(set(longitudinal_dataset['id'])))
    indx_train, indx_test = train_test_split(indx, test_size=0.1)

    longitudinal_dataset_train = longitudinal_dataset[longitudinal_dataset['id'].isin(indx_train)]
    static_features_train = static_features[static_features['id'].isin(indx_train)]

    longitudinal_dataset_test = longitudinal_dataset[longitudinal_dataset['id'].isin(indx_test)]
    static_features_test = static_features[static_features['id'].isin(indx_test)]

    print("Finished data pre-processing.")

    longitudinal_dataset_train.to_csv("../data/mimic_antibiotics/mimic_antibiotics_temporal_train_data_eav.csv.gz",
                                      sep=',',
                                      compression='gzip',
                                      index=False)
    static_features_train.to_csv("../data/mimic_antibiotics/mimic_antibiotics_static_train_data.csv.gz", sep=',',
                                 compression='gzip',
                                 index=False)

    longitudinal_dataset_test.to_csv("../data/mimic_antibiotics/mimic_antibiotics_temporal_test_data_eav.csv.gz",
                                     sep=',',
                                     compression='gzip',
                                     index=False)
    static_features_test.to_csv("../data/mimic_antibiotics/mimic_antibiotics_static_test_data.csv.gz", sep=',',
                                compression='gzip',
                                index=False)

    print ("Saved dataset.")


if __name__ == '__main__':
    extract_antibiotics_dataset()
